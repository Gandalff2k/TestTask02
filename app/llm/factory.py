from dataclasses import dataclass

import httpx

from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.keyless import KeylessProvider
from app.llm.openai_compatible import OpenAICompatibleProvider


class LLMConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    base_url: str | None
    default_model: str | None = None


PROVIDERS: dict[str, ProviderPreset] = {
    "groq": ProviderPreset("https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    "gemini": ProviderPreset(
        "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"
    ),
    "openrouter": ProviderPreset("https://openrouter.ai/api/v1"),
    "openai": ProviderPreset("https://api.openai.com/v1"),
    "custom": ProviderPreset(None),
}


def build_provider(settings: Settings, client: httpx.AsyncClient) -> LLMProvider:
    preset = PROVIDERS.get(settings.llm_provider)
    if preset is None:
        raise LLMConfigError(
            f"Unknown LLM_PROVIDER {settings.llm_provider!r}, expected one of {sorted(PROVIDERS)}"
        )

    base_url = settings.llm_base_url or preset.base_url
    model = settings.llm_model or preset.default_model
    if base_url is None:
        raise LLMConfigError(f"LLM_BASE_URL is required for {settings.llm_provider}")
    if model is None:
        raise LLMConfigError(f"LLM_MODEL is required for {settings.llm_provider}")

    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    provider = OpenAICompatibleProvider(
        client=client,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    if api_key is None:
        return KeylessProvider(
            provider,
            delay_min_seconds=settings.llm_keyless_delay_min_seconds,
            delay_max_seconds=settings.llm_keyless_delay_max_seconds,
        )
    return provider
