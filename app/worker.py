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
from app.delivery import DeliveryOutcome, DeliveryResult, parse_allowed_hosts, send_reply
from app.drafts import Draft, DraftFormatError, generate_draft
from app.llm.base import LLMPermanentError, LLMProvider, LLMRetryableError
from app.llm.factory import build_provider
from app.llm.rate_limit import PostgresRateLimiter, RateLimitedProvider
from app.logs import configure_logging
from app.tickets import (
    ClaimedDelivery,
    ClaimedTicket,
    claim_for_delivery,
    claim_for_generation,
    expire_stale_deliveries,
    fail_delivery,
    fail_generation,
    mark_delivery_unknown,
    mark_sent,
    reschedule_delivery,
    reschedule_generation,
    save_draft,
)

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500
PROCESSING_SHARE_OF_LEASE = 0.8


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def backoff_delay(
    attempts: int, base_seconds: float, max_seconds: float, rng: random.Random
) -> float:
    return min(base_seconds * 2.0 ** (attempts - 1), max_seconds) * rng.uniform(0.8, 1.2)


class QueueWorker[Item: (ClaimedTicket, ClaimedDelivery)]:
    name = "queue"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        concurrency: int,
        poll_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._concurrency = concurrency
        self._poll_seconds = poll_seconds

    async def claim(self, session: AsyncSession, limit: int) -> list[Item]:
        raise NotImplementedError

    async def process(self, item: Item) -> None:
        raise NotImplementedError

    async def run(self, stop: asyncio.Event) -> None:
        active: set[asyncio.Task[None]] = set()
        logger.info("%s_worker_started concurrency=%d", self.name, self._concurrency)
        async with asyncio.TaskGroup() as group:
            while not stop.is_set():
                claimed = await self._claim(self._concurrency - len(active))
                for item in claimed:
                    task = group.create_task(self.process(item))
                    active.add(task)
                    task.add_done_callback(active.discard)
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            logger.info("%s_worker_stopping in_flight=%d", self.name, len(active))
        logger.info("%s_worker_stopped", self.name)

    async def _claim(self, free_slots: int) -> list[Item]:
        if free_slots <= 0:
            return []
        try:
            async with self._session_factory() as session:
                claimed = await self.claim(session, free_slots)
                await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("%s_claim_failed error=%s", self.name, describe(exc))
            return []
        return claimed

    async def _write(
        self, ticket_id: str, operation: Callable[[AsyncSession], Awaitable[bool]]
    ) -> bool:
        try:
            async with self._session_factory() as session:
                applied = await operation(session)
                await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.warning(
                "%s_write_failed ticket_id=%r error=%s", self.name, ticket_id, describe(exc)
            )
            return False
        if not applied:
            logger.warning("%s_lease_lost ticket_id=%r", self.name, ticket_id)
        return applied


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    concurrency: int
    poll_seconds: float
    lease_seconds: float
    max_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float
    public_base_url: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "GenerationConfig":
        return cls(
            concurrency=settings.worker_concurrency,
            poll_seconds=settings.worker_poll_seconds,
            lease_seconds=settings.generation_lease_seconds,
            max_attempts=settings.generation_max_attempts,
            retry_base_seconds=settings.generation_retry_base_seconds,
            retry_max_seconds=settings.generation_retry_max_seconds,
            public_base_url=settings.public_base_url,
        )


class GenerationWorker(QueueWorker[ClaimedTicket]):
    name = "generation"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: LLMProvider,
        config: GenerationConfig,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(session_factory, config.concurrency, config.poll_seconds)
        self._provider = provider
        self._config = config
        self._rng = rng or random.Random()  # noqa: S311

    async def claim(self, session: AsyncSession, limit: int) -> list[ClaimedTicket]:
        return await claim_for_generation(
            session,
            limit=limit,
            lease_seconds=self._config.lease_seconds,
            max_attempts=self._config.max_attempts,
        )

    async def process(self, item: ClaimedTicket) -> None:
        logger.info(
            "generation_started ticket_id=%r event_id=%r attempt=%d",
            item.ticket_id,
            item.event_id,
            item.attempts,
        )
        try:
            async with asyncio.timeout(self._config.lease_seconds * PROCESSING_SHARE_OF_LEASE):
                draft = await generate_draft(
                    self._session_factory,
                    self._provider,
                    item.message,
                    self._config.public_base_url,
                )
        except LLMPermanentError as exc:
            await self._fail(item, exc)
        except (LLMRetryableError, DraftFormatError, TimeoutError) as exc:
            await self._retry(item, exc)
        except Exception as exc:
            logger.exception("generation_crashed ticket_id=%r", item.ticket_id)
            await self._retry(item, exc)
        else:
            await self._save(item, draft)

    async def _save(self, ticket: ClaimedTicket, draft: Draft) -> None:
        saved = await self._write(
            ticket.ticket_id, lambda s: save_draft(s, ticket, draft.text, draft.sources)
        )
        logger.info(
            "generation_finished ticket_id=%r sources=%d saved=%s",
            ticket.ticket_id,
            len(draft.sources),
            saved,
        )

    async def _fail(self, ticket: ClaimedTicket, exc: BaseException) -> None:
        await self._write(ticket.ticket_id, lambda s: fail_generation(s, ticket, describe(exc)))
        logger.warning("generation_failed ticket_id=%r error=%s", ticket.ticket_id, describe(exc))

    async def _retry(self, ticket: ClaimedTicket, exc: BaseException) -> None:
        # A provider-wide rate limit says nothing about this ticket, so it must not use up attempts.
        rate_limited = isinstance(exc, LLMRetryableError) and exc.rate_limited
        if ticket.attempts >= self._config.max_attempts and not rate_limited:
            await self._fail(ticket, exc)
            return
        if isinstance(exc, LLMRetryableError) and exc.retry_after_seconds is not None:
            delay = min(exc.retry_after_seconds, self._config.retry_max_seconds)
        else:
            delay = backoff_delay(
                ticket.attempts,
                self._config.retry_base_seconds,
                self._config.retry_max_seconds,
                self._rng,
            )
        await self._write(
            ticket.ticket_id,
            lambda s: reschedule_generation(
                s, ticket, delay, describe(exc), refund_attempt=rate_limited
            ),
        )
        logger.warning(
            "generation_retry_scheduled ticket_id=%r delay=%.1f error=%s",
            ticket.ticket_id,
            delay,
            describe(exc),
        )


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    concurrency: int
    poll_seconds: float
    lease_seconds: float
    max_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float
    connect_timeout_seconds: float
    read_timeout_seconds: float
    allowed_hosts: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> "DeliveryConfig":
        return cls(
            concurrency=settings.delivery_concurrency,
            poll_seconds=settings.worker_poll_seconds,
            lease_seconds=settings.delivery_lease_seconds,
            max_attempts=settings.delivery_max_attempts,
            retry_base_seconds=settings.delivery_retry_base_seconds,
            retry_max_seconds=settings.delivery_retry_max_seconds,
            connect_timeout_seconds=settings.delivery_connect_timeout_seconds,
            read_timeout_seconds=settings.delivery_read_timeout_seconds,
            allowed_hosts=parse_allowed_hosts(settings.helpdesk_allowed_hosts),
        )


class DeliveryWorker(QueueWorker[ClaimedDelivery]):
    name = "delivery"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        config: DeliveryConfig,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(session_factory, config.concurrency, config.poll_seconds)
        self._client = client
        self._config = config
        self._rng = rng or random.Random()  # noqa: S311

    async def claim(self, session: AsyncSession, limit: int) -> list[ClaimedDelivery]:
        for ticket_id in await expire_stale_deliveries(session):
            logger.warning("delivery_abandoned_mid_request id=%d", ticket_id)
        return await claim_for_delivery(session, limit, self._config.lease_seconds)

    async def process(self, item: ClaimedDelivery) -> None:
        logger.info("delivery_started ticket_id=%r attempt=%d", item.ticket_id, item.attempts)
        if item.draft_text is None:
            result = DeliveryResult(DeliveryOutcome.FAILED, "approved ticket has no draft")
        else:
            result = await self._send(item, item.draft_text)

        if result.outcome is DeliveryOutcome.RETRY and item.attempts >= self._config.max_attempts:
            result = DeliveryResult(DeliveryOutcome.FAILED, f"retries exhausted, {result.detail}")

        match result.outcome:
            case DeliveryOutcome.DELIVERED:
                await self._write(item.ticket_id, lambda s: mark_sent(s, item))
            case DeliveryOutcome.RETRY:
                delay = self._retry_delay(item, result)
                await self._write(
                    item.ticket_id, lambda s: reschedule_delivery(s, item, delay, result.detail)
                )
            case DeliveryOutcome.FAILED:
                await self._write(item.ticket_id, lambda s: fail_delivery(s, item, result.detail))
            case DeliveryOutcome.UNKNOWN:
                await self._write(
                    item.ticket_id, lambda s: mark_delivery_unknown(s, item, result.detail)
                )
        logger.info(
            "delivery_finished ticket_id=%r outcome=%s detail=%s",
            item.ticket_id,
            result.outcome,
            result.detail,
        )

    async def _send(self, item: ClaimedDelivery, message: str) -> DeliveryResult:
        try:
            async with asyncio.timeout(self._config.lease_seconds * PROCESSING_SHARE_OF_LEASE):
                return await send_reply(
                    self._client,
                    item.reply_url,
                    item.ticket_id,
                    message,
                    self._config.allowed_hosts,
                    self._config.connect_timeout_seconds,
                    self._config.read_timeout_seconds,
                )
        except TimeoutError:
            return DeliveryResult(DeliveryOutcome.UNKNOWN, "no response within the lease")
        except Exception as exc:
            logger.exception("delivery_crashed ticket_id=%r", item.ticket_id)
            return DeliveryResult(DeliveryOutcome.UNKNOWN, describe(exc))

    def _retry_delay(self, item: ClaimedDelivery, result: DeliveryResult) -> float:
        if result.retry_after_seconds is not None:
            return min(result.retry_after_seconds, self._config.retry_max_seconds)
        return backoff_delay(
            item.attempts,
            self._config.retry_base_seconds,
            self._config.retry_max_seconds,
            self._rng,
        )


async def main() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with httpx.AsyncClient() as llm_client, httpx.AsyncClient() as helpdesk_client:
            provider = RateLimitedProvider(
                build_provider(settings, llm_client),
                PostgresRateLimiter(session_factory, settings.llm_rate_limit_per_minute),
            )
            generation = GenerationWorker(
                session_factory, provider, GenerationConfig.from_settings(settings)
            )
            delivery = DeliveryWorker(
                session_factory, helpdesk_client, DeliveryConfig.from_settings(settings)
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(generation.run(stop))
                group.create_task(delivery.run(stop))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(main())
