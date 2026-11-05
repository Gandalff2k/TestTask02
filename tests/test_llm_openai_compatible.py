import json
from collections.abc import Callable

import httpx
import pytest

from app.llm.base import LLMAuthError, LLMPermanentError, LLMRequest, LLMRetryableError
from app.llm.openai_compatible import OpenAICompatibleProvider

REQUEST = LLMRequest(system="sys", user="usr")


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response], api_key: str | None = "secret"
) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        client=client,
        base_url="https://llm.test/v1/",
        model="test-model",
        api_key=api_key,
        timeout_seconds=1.0,
    )


async def test_sends_openai_format_and_parses_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    response = await make_provider(handler).complete(REQUEST)

    assert response.text == "hello"
    assert response.model == "test-model"
    assert str(seen[0].url) == "https://llm.test/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer secret"
    body = json.loads(seen[0].content)
    assert body["model"] == "test-model"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "usr"}
    assert body["response_format"] == {"type": "json_object"}


async def test_omits_auth_header_without_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    await make_provider(handler, api_key=None).complete(REQUEST)

    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_auth_error(status: int) -> None:
    provider = make_provider(lambda _: httpx.Response(status))

    with pytest.raises(LLMAuthError):
        await provider.complete(REQUEST)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_status_is_retryable(status: int) -> None:
    provider = make_provider(lambda _: httpx.Response(status))

    with pytest.raises(LLMRetryableError):
        await provider.complete(REQUEST)


async def test_retry_after_header_is_exposed() -> None:
    provider = make_provider(lambda _: httpx.Response(429, headers={"Retry-After": "7"}))

    with pytest.raises(LLMRetryableError) as exc_info:
        await provider.complete(REQUEST)

    assert exc_info.value.retry_after_seconds == 7.0


async def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMRetryableError):
        await make_provider(handler).complete(REQUEST)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
    ],
)
async def test_bad_request_or_malformed_body_is_permanent(response: httpx.Response) -> None:
    provider = make_provider(lambda _: response)

    with pytest.raises(LLMPermanentError):
        await provider.complete(REQUEST)


async def test_errors_do_not_leak_api_key() -> None:
    provider = make_provider(lambda _: httpx.Response(401, text="bad key secret"))

    with pytest.raises(LLMAuthError) as exc_info:
        await provider.complete(REQUEST)

    assert "secret" not in str(exc_info.value)
