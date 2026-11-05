import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable

from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

STATIC_MODEL = "static"
STATIC_TEXT = json.dumps(
    {
        "reply": (
            "Дякуємо за звернення! Ми переглянули ваше питання і підібрали статті бази знань, "
            "де описано можливе рішення. Будь ласка, скористайтеся ними. Якщо це не допоможе, "
            "напишіть нам, і ми розберемося далі."
        ),
        "article_ids": [],
    },
    ensure_ascii=False,
)


class KeylessProvider:
    def __init__(
        self,
        inner: LLMProvider,
        delay_min_seconds: float,
        delay_max_seconds: float,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._delay_min_seconds = delay_min_seconds
        self._delay_max_seconds = delay_max_seconds
        self._rng = rng or random.Random()  # noqa: S311
        self._sleep = sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        delay = self._rng.uniform(self._delay_min_seconds, self._delay_max_seconds)
        started = time.monotonic()
        try:
            async with asyncio.timeout(delay):
                await self._inner.complete(request)
        except (LLMError, TimeoutError) as exc:
            logger.info("keyless_request_finished result=%s", type(exc).__name__)

        remaining = delay - (time.monotonic() - started)
        if remaining > 0:
            await self._sleep(remaining)
        return LLMResponse(text=STATIC_TEXT, model=STATIC_MODEL)
