import asyncio
import contextlib
import logging
import random
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.drafts import Draft, DraftFormatError, generate_draft
from app.llm.base import LLMPermanentError, LLMProvider, LLMRetryableError
from app.llm.factory import build_provider
from app.llm.rate_limit import PostgresRateLimiter, RateLimitedProvider
from app.logs import configure_logging
from app.tickets import (
    ClaimedTicket,
    claim_for_generation,
    fail_generation,
    reschedule_generation,
    save_draft,
)

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500
PROCESSING_SHARE_OF_LEASE = 0.8


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    concurrency: int
    poll_seconds: float
    lease_seconds: float
    max_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float
    public_base_url: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "WorkerConfig":
        return cls(
            concurrency=settings.worker_concurrency,
            poll_seconds=settings.worker_poll_seconds,
            lease_seconds=settings.generation_lease_seconds,
            max_attempts=settings.generation_max_attempts,
            retry_base_seconds=settings.generation_retry_base_seconds,
            retry_max_seconds=settings.generation_retry_max_seconds,
            public_base_url=settings.public_base_url,
        )


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


class GenerationWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: LLMProvider,
        config: WorkerConfig,
        rng: random.Random | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._config = config
        self._rng = rng or random.Random()  # noqa: S311

    async def run(self, stop: asyncio.Event) -> None:
        active: set[asyncio.Task[None]] = set()
        logger.info("generation_worker_started concurrency=%d", self._config.concurrency)
        async with asyncio.TaskGroup() as group:
            while not stop.is_set():
                claimed = await self._claim(self._config.concurrency - len(active))
                for ticket in claimed:
                    task = group.create_task(self.process(ticket))
                    active.add(task)
                    task.add_done_callback(active.discard)
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self._config.poll_seconds)
            logger.info("generation_worker_stopping in_flight=%d", len(active))
        logger.info("generation_worker_stopped")

    async def _claim(self, free_slots: int) -> list[ClaimedTicket]:
        if free_slots <= 0:
            return []
        try:
            async with self._session_factory() as session:
                claimed = await claim_for_generation(
                    session,
                    limit=free_slots,
                    lease_seconds=self._config.lease_seconds,
                    max_attempts=self._config.max_attempts,
                )
                await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("generation_claim_failed error=%s", describe(exc))
            return []
        return claimed

    async def process(self, ticket: ClaimedTicket) -> None:
        logger.info(
            "generation_started ticket_id=%r event_id=%r attempt=%d",
            ticket.ticket_id,
            ticket.event_id,
            ticket.attempts,
        )
        try:
            async with asyncio.timeout(self._config.lease_seconds * PROCESSING_SHARE_OF_LEASE):
                draft = await generate_draft(
                    self._session_factory,
                    self._provider,
                    ticket.message,
                    self._config.public_base_url,
                )
        except LLMPermanentError as exc:
            await self._fail(ticket, exc)
        except (LLMRetryableError, DraftFormatError, TimeoutError) as exc:
            await self._retry(ticket, exc)
        except Exception as exc:
            logger.exception("generation_crashed ticket_id=%r", ticket.ticket_id)
            await self._retry(ticket, exc)
        else:
            await self._save(ticket, draft)

    async def _save(self, ticket: ClaimedTicket, draft: Draft) -> None:
        saved = await self._write(
            ticket, lambda s: save_draft(s, ticket, draft.text, draft.sources)
        )
        logger.info(
            "generation_finished ticket_id=%r sources=%d saved=%s",
            ticket.ticket_id,
            len(draft.sources),
            saved,
        )

    async def _fail(self, ticket: ClaimedTicket, exc: BaseException) -> None:
        await self._write(ticket, lambda s: fail_generation(s, ticket, describe(exc)))
        logger.warning("generation_failed ticket_id=%r error=%s", ticket.ticket_id, describe(exc))

    async def _retry(self, ticket: ClaimedTicket, exc: BaseException) -> None:
        if ticket.attempts >= self._config.max_attempts:
            await self._fail(ticket, exc)
            return
        delay = self._retry_delay(ticket.attempts, exc)
        await self._write(ticket, lambda s: reschedule_generation(s, ticket, delay, describe(exc)))
        logger.warning(
            "generation_retry_scheduled ticket_id=%r delay=%.1f error=%s",
            ticket.ticket_id,
            delay,
            describe(exc),
        )

    def _retry_delay(self, attempts: int, exc: BaseException) -> float:
        if isinstance(exc, LLMRetryableError) and exc.retry_after_seconds is not None:
            return min(exc.retry_after_seconds, self._config.retry_max_seconds)
        backoff = self._config.retry_base_seconds * 2.0 ** (attempts - 1)
        return min(backoff, self._config.retry_max_seconds) * self._rng.uniform(0.8, 1.2)

    async def _write(
        self,
        ticket: ClaimedTicket,
        operation: Callable[[AsyncSession], Awaitable[bool]],
    ) -> bool:
        try:
            async with self._session_factory() as session:
                applied = await operation(session)
                await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.warning(
                "generation_write_failed ticket_id=%r error=%s", ticket.ticket_id, describe(exc)
            )
            return False
        if not applied:
            logger.warning("generation_lease_lost ticket_id=%r", ticket.ticket_id)
        return applied


async def main() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with httpx.AsyncClient() as client:
            provider = RateLimitedProvider(
                build_provider(settings, client),
                PostgresRateLimiter(session_factory, settings.llm_rate_limit_per_minute),
            )
            worker = GenerationWorker(
                session_factory, provider, WorkerConfig.from_settings(settings)
            )
            await worker.run(stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(main())
