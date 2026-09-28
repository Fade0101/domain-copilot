"""Application configuration (BRD AR-4).

Typed settings loaded from the environment / ``.env`` via pydantic-settings.
Configuration is an *edge* concern, so pydantic is permitted here (and in the
presentation schemas) but nowhere in domain/application. Secrets are never
hard-coded: values come from the environment, keeping them out of code and git
history (constraint C6). ``extra="ignore"`` lets the shared ``.env`` carry
variables owned by future tickets (database, redis, auth) without breaking here.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Additional fields are added by the tickets that need them."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Domain Copilot"
    environment: str = "development"
    api_v1_str: str = "/api/v1"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` instance."""
    return Settings()
