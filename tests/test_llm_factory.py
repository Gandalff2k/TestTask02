import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.llm.factory import PROVIDERS, LLMConfigError, build_provider
from app.llm.keyless import KeylessProvider
from app.llm.openai_compatible import OpenAICompatibleProvider


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"llm_api_key": None, "llm_model": None, "llm_base_url": None}
    return Settings.model_validate(defaults | overrides)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def test_without_key_wraps_in_keyless(client: httpx.AsyncClient) -> None:
    provider = build_provider(settings(llm_provider="groq"), client)

    assert isinstance(provider, KeylessProvider)


def test_blank_key_counts_as_no_key(client: httpx.AsyncClient) -> None:
    provider = build_provider(settings(llm_provider="groq", llm_api_key="  "), client)

    assert isinstance(provider, KeylessProvider)


@pytest.mark.parametrize("name", ["groq", "gemini"])
def test_with_key_uses_real_provider(name: str, client: httpx.AsyncClient) -> None:
    config = settings(llm_provider=name, llm_api_key=SecretStr("key"))

    assert isinstance(build_provider(config, client), OpenAICompatibleProvider)


def test_unknown_provider_is_rejected(client: httpx.AsyncClient) -> None:
    with pytest.raises(LLMConfigError, match="nope"):
        build_provider(settings(llm_provider="nope"), client)


def test_custom_requires_base_url_and_model(client: httpx.AsyncClient) -> None:
    with pytest.raises(LLMConfigError, match="LLM_BASE_URL"):
        build_provider(settings(llm_provider="custom", llm_model="m"), client)
    with pytest.raises(LLMConfigError, match="LLM_MODEL"):
        build_provider(settings(llm_provider="custom", llm_base_url="http://x/v1"), client)


def test_custom_works_with_explicit_url_and_model(client: httpx.AsyncClient) -> None:
    config = settings(
        llm_provider="custom",
        llm_base_url="http://ollama:11434/v1",
        llm_model="qwen",
        llm_api_key=SecretStr("key"),
    )

    assert isinstance(build_provider(config, client), OpenAICompatibleProvider)


def test_presets_without_default_model_require_one(client: httpx.AsyncClient) -> None:
    missing = [
        name
        for name, preset in PROVIDERS.items()
        if preset.base_url is not None and preset.default_model is None
    ]

    assert missing
    for name in missing:
        with pytest.raises(LLMConfigError, match="LLM_MODEL"):
            build_provider(settings(llm_provider=name), client)
