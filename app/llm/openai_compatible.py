import httpx
from pydantic import BaseModel, ValidationError

from app.llm.base import (
    LLMAuthError,
    LLMPermanentError,
    LLMRequest,
    LLMResponse,
    LLMRetryableError,
)

_RETRYABLE_STATUSES = {408, 409, 425, 429}


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _Message


class _Completion(BaseModel):
    choices: list[_Choice]


class OpenAICompatibleProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.post(
                self._url,
                json=self._payload(request),
                headers=self._headers(),
                timeout=self._timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise LLMRetryableError(f"transport error: {type(exc).__name__}") from exc

        self._raise_for_status(response)
        return LLMResponse(text=self._parse_text(response), model=self._model)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _payload(self, request: LLMRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if response.is_success:
            return
        if status in (401, 403):
            raise LLMAuthError(f"provider rejected credentials: {status}")
        if status in _RETRYABLE_STATUSES or status >= 500:
            raise LLMRetryableError(
                f"provider returned {status}",
                retry_after_seconds=_parse_retry_after(response),
            )
        raise LLMPermanentError(f"provider returned {status}")

    @staticmethod
    def _parse_text(response: httpx.Response) -> str:
        try:
            completion = _Completion.model_validate_json(response.content)
        except ValidationError as exc:
            raise LLMPermanentError("provider returned malformed body") from exc
        if not completion.choices or not completion.choices[0].message.content.strip():
            raise LLMPermanentError("provider returned empty completion")
        return completion.choices[0].message.content


def _parse_retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None
