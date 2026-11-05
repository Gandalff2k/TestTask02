import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.base import LLMProvider, LLMRequest, LLMResponse

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


class RateLimiter(Protocol):
    async def acquire(self) -> None: ...


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


class RateLimitedProvider:
    def __init__(self, inner: LLMProvider, limiter: RateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await self._limiter.acquire()
        logger.info("llm_request_started")
        return await self._inner.complete(request)
