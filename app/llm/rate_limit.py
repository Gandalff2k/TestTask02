import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMRetryableError

logger = logging.getLogger(__name__)

# Requests leave a few ms after their slot; without a margin a strict sliding window
# at the provider could occasionally count 61 requests in 60 seconds.
SLOT_MARGIN_SECONDS = 0.05

_TAKE_SLOT_SQL = text(
    """
    UPDATE llm_rate_limit
    SET next_slot = greatest(next_slot, clock_timestamp())
        + make_interval(secs => CAST(:interval AS double precision))
    WHERE id = 1
    RETURNING greatest(
        0,
        extract(epoch FROM next_slot - clock_timestamp()) - CAST(:interval AS double precision)
    ) AS wait_seconds
    """
)


_PAUSE_SQL = text(
    """
    UPDATE llm_rate_limit
    SET next_slot = greatest(
        next_slot,
        clock_timestamp() + make_interval(secs => CAST(:seconds AS double precision))
    )
    WHERE id = 1
    """
)

DEFAULT_PAUSE_SECONDS = 10.0
MAX_PAUSE_SECONDS = 3600.0


class RateLimiter(Protocol):
    async def acquire(self) -> None: ...

    async def pause(self, seconds: float) -> None: ...


class PostgresRateLimiter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        requests_per_minute: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._interval = 60.0 / requests_per_minute + SLOT_MARGIN_SECONDS
        self._sleep = sleep

    async def acquire(self) -> None:
        async with self._session_factory() as session:
            wait = await session.scalar(_TAKE_SLOT_SQL, {"interval": self._interval})
            await session.commit()
        if wait is None:
            raise RuntimeError("llm_rate_limit row is missing, run migrations")
        if wait > 0:
            await self._sleep(float(wait))

    async def pause(self, seconds: float) -> None:
        async with self._session_factory() as session:
            await session.execute(_PAUSE_SQL, {"seconds": seconds})
            await session.commit()


class RateLimitedProvider:
    def __init__(self, inner: LLMProvider, limiter: RateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await self._limiter.acquire()
        logger.info("llm_request_started")
        try:
            return await self._inner.complete(request)
        except LLMRetryableError as exc:
            if exc.rate_limited:
                await self._pause_everyone(exc.retry_after_seconds)
            raise

    async def _pause_everyone(self, retry_after_seconds: float | None) -> None:
        seconds = min(retry_after_seconds or DEFAULT_PAUSE_SECONDS, MAX_PAUSE_SECONDS)
        logger.warning("llm_rate_limited pause=%.1f", seconds)
        try:
            await self._limiter.pause(seconds)
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("llm_pause_failed error=%s", type(exc).__name__)
