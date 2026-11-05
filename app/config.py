from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    log_level: str = "INFO"

    webhook_db_timeout_seconds: float = 1.5
    max_body_bytes: int = 1_048_576
    kb_dir: Path = Path("kb")
    public_base_url: str = "http://localhost:8000"
    operator_username: str = "operator"
    operator_password: SecretStr | None = None

    worker_concurrency: int = 30
    worker_poll_seconds: float = 1.0
    generation_lease_seconds: float = 180.0
    generation_max_attempts: int = 5
    generation_retry_base_seconds: float = 5.0
    generation_retry_max_seconds: float = 300.0

    llm_provider: str = "groq"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = 60.0
    llm_rate_limit_per_minute: int = 60
    llm_keyless_delay_min_seconds: float = 5.0
    llm_keyless_delay_max_seconds: float = 30.0

    @field_validator("llm_api_key", "llm_model", "llm_base_url", "operator_password", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
