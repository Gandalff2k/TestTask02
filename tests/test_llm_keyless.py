import asyncio
import json
import random

import pytest

from app.llm.base import LLMAuthError, LLMError, LLMRequest, LLMResponse, LLMRetryableError
from app.llm.keyless import KeylessProvider

REQUEST = LLMRequest(system="sys", user="usr")


class RecordingProvider:
    def __init__(self, error: LLMError | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self.error = error

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        return LLMResponse(text="real answer", model="real")


class HangingProvider:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.mark.parametrize(
    "error", [None, LLMAuthError("401"), LLMRetryableError("timeout")], ids=str
)
async def test_makes_real_request_and_returns_static_answer(error: LLMError | None) -> None:
    inner = RecordingProvider(error)
    sleep = FakeSleep()
    provider = KeylessProvider(inner, 5.0, 30.0, rng=random.Random(1), sleep=sleep)  # noqa: S311

    response = await provider.complete(REQUEST)

    assert inner.requests == [REQUEST]
    assert response.model == "static"
    assert json.loads(response.text).keys() == {"reply", "article_ids"}


async def test_delay_stays_within_bounds() -> None:
    sleep = FakeSleep()
    provider = KeylessProvider(
        RecordingProvider(LLMAuthError("401")),
        5.0,
        30.0,
        rng=random.Random(1),  # noqa: S311
        sleep=sleep,
    )

    for _ in range(50):
        await provider.complete(REQUEST)

    assert len(sleep.calls) == 50
    assert all(4.9 < seconds < 30.0 for seconds in sleep.calls)


async def test_hanging_provider_does_not_extend_delay() -> None:
    provider = KeylessProvider(HangingProvider(), 0.01, 0.02)

    response = await asyncio.wait_for(provider.complete(REQUEST), timeout=1.0)

    assert response.model == "static"
