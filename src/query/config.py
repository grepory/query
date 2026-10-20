"""Explicit application and model settings; no import-time configuration."""

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUERY_", env_file=".env", extra="ignore")

    database_url: PostgresDsn
    firecrawl_api_key: SecretStr | None = None
    exa_api_key: SecretStr | None = None


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUERY_", env_file=".env", extra="ignore")

    model_backend: Literal["anthropic", "openai", "local"]
    model_name: str
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    local_base_url: AnyHttpUrl | None = None
    local_api_key: SecretStr = SecretStr("local")

    @model_validator(mode="after")
    def require_provider_configuration(self) -> ModelSettings:
        if not self.model_name.strip():
            raise ValueError("QUERY_MODEL_NAME must name an explicit model")
        if self.model_backend == "local":
            if self.local_base_url is None:
                raise ValueError("QUERY_LOCAL_BASE_URL is required for local models")
        else:
            key = getattr(self, f"{self.model_backend}_api_key")
            if key is None or not key.get_secret_value():
                raise ValueError(f"QUERY_{self.model_backend.upper()}_API_KEY is required")
        return self


class SmokeAnswer(BaseModel):
    """Small structured-output contract for the opt-in plumbing eval."""

    answer: str
