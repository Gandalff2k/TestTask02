from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    user: str
    max_output_tokens: int = 1024
    temperature: float = 0.2
    json_mode: bool = True


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str


class LLMError(Exception):
    pass


class LLMRetryableError(LLMError):
    def __init__(
        self,
        message: str,
        retry_after_seconds: float | None = None,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.rate_limited = rate_limited


class LLMPermanentError(LLMError):
    pass


class LLMAuthError(LLMPermanentError):
    pass


class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
