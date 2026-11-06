import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.base import LLMRequest, LLMResponse, LLMRetryableError
from app.llm.rate_limit import (
    DEFAULT_PAUSE_SECONDS,
    MAX_PAUSE_SECONDS,
    SLOT_MARGIN_SECONDS,
    PostgresRateLimiter,
    RateLimitedProvider,
)


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def test_slots_are_spaced_across_limiter_instances(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sleep = RecordingSleep()
    limiters = [PostgresRateLimiter(session_factory, 60, sleep=sleep) for _ in range(3)]

    await asyncio.gather(*(limiters[i % 3].acquire() for i in range(12)))

    interval = 1.0 + SLOT_MARGIN_SECONDS
    waits = sorted(sleep.calls)
    assert len(waits) == 11
    assert waits[-1] == pytest.approx(11 * interval, abs=0.5)
    gaps = [later - earlier for earlier, later in zip([0.0, *waits], waits, strict=False)]
    assert all(gap == pytest.approx(interval, abs=0.1) for gap in gaps)


async def test_at_most_limit_slots_in_any_minute(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sleep = RecordingSleep()
    limiter = PostgresRateLimiter(session_factory, 60, sleep=sleep)

    for _ in range(61):
        await limiter.acquire()

    assert max(sleep.calls) > 60.0


async def test_idle_limiter_does_not_wait(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(text("UPDATE llm_rate_limit SET next_slot = now() - interval '1h'"))
        await session.commit()
    sleep = RecordingSleep()

    await PostgresRateLimiter(session_factory, 60, sleep=sleep).acquire()

    assert sleep.calls == []


async def test_missing_row_is_an_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(text("DELETE FROM llm_rate_limit"))
        await session.commit()
    try:
        with pytest.raises(RuntimeError, match="migrations"):
            await PostgresRateLimiter(session_factory, 60).acquire()
    finally:
        async with session_factory() as session:
            await session.execute(text("INSERT INTO llm_rate_limit (id) VALUES (1)"))
            await session.commit()


async def test_provider_takes_a_slot_for_every_call() -> None:
    events: list[str] = []

    class Limiter:
        async def acquire(self) -> None:
            events.append("slot")

    class Provider:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            events.append("call")
            return LLMResponse(text="ok", model="m")

    provider = RateLimitedProvider(Provider(), Limiter())

    await provider.complete(LLMRequest(system="s", user="u"))
    await provider.complete(LLMRequest(system="s", user="u"))

    assert events == ["slot", "call", "slot", "call"]


async def test_pause_delays_every_limiter_sharing_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sleep = RecordingSleep()
    first = PostgresRateLimiter(session_factory, 60, sleep=sleep)
    second = PostgresRateLimiter(session_factory, 60, sleep=sleep)

    await first.pause(30)
    await second.acquire()

    assert sleep.calls == [pytest.approx(30, abs=0.5)]


async def test_pause_never_shortens_an_existing_wait(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sleep = RecordingSleep()
    limiter = PostgresRateLimiter(session_factory, 60, sleep=sleep)

    await limiter.pause(30)
    await limiter.pause(5)
    await limiter.acquire()

    assert sleep.calls == [pytest.approx(30, abs=0.5)]


class PausingLimiter:
    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def acquire(self) -> None:
        return None

    async def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)


class FailingProvider:
    def __init__(self, error: LLMRetryableError) -> None:
        self.error = error

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected_pauses"),
    [
        (LLMRetryableError("429", retry_after_seconds=7, rate_limited=True), [7]),
        (LLMRetryableError("429", rate_limited=True), [DEFAULT_PAUSE_SECONDS]),
        (
            LLMRetryableError("429", retry_after_seconds=86_400, rate_limited=True),
            [MAX_PAUSE_SECONDS],
        ),
        (LLMRetryableError("503", retry_after_seconds=7), []),
    ],
    ids=["retry_after", "no_header", "capped", "server_error"],
)
async def test_rate_limit_pauses_everyone_and_reraises(
    error: LLMRetryableError, expected_pauses: list[float]
) -> None:
    limiter = PausingLimiter()
    provider = RateLimitedProvider(FailingProvider(error), limiter)

    with pytest.raises(LLMRetryableError):
        await provider.complete(LLMRequest(system="s", user="u"))

    assert limiter.pauses == expected_pauses
