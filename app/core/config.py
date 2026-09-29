"""Application configuration (BRD AR-4).

Typed settings loaded from the environment / ``.env`` via pydantic-settings.
Configuration is an *edge* concern, so pydantic is permitted here (and in the
presentation schemas) but nowhere in domain/application. Secrets are never
hard-coded: values come from the environment, keeping them out of code and git
history (constraint C6), and API keys use :class:`~pydantic.SecretStr` so they
are masked in logs and ``repr``.

Grouped settings are nested pydantic models populated with the ``__`` delimiter,
so e.g. ``LLM__MODEL`` sets ``settings.llm.model`` and ``RETRY__MAX_RETRIES``
sets ``settings.retry.max_retries``. Every field has a safe default so the app
boots without a ``.env`` in development; real providers/brokers/keys are supplied
by the environment in each deployment. ``extra="ignore"`` lets the shared
``.env`` carry variables owned by other tickets (auth, database) without breaking
here. Concrete provider *adapters* that consume this config land in their own
tickets (#7 providers, #9 vector store, #20 queue); this ticket owns the config
surface itself.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseModel):
    """Chat/completion provider configuration (adapter finalized in #7)."""

    provider: str = "groq"
    model: str = "llama-3.1-8b-instant"
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_seconds: int = 60
    api_key: SecretStr | None = None


class EmbeddingSettings(BaseModel):
    """Text-embedding provider configuration (adapter finalized in #7)."""

    provider: str = "sentence_transformers"
    model: str = "all-MiniLM-L6-v2"
    dimensions: int = 384
    batch_size: int = 32


class QueueSettings(BaseModel):
    """Async job queue configuration (T7; broker adapter finalized in #20)."""

    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/1"
    default_queue: str = "default"
    visibility_timeout_seconds: int = 3600


class RetrievalSettings(BaseModel):
    """Dense-retrieval tuning (vector store adapter finalized in #9)."""

    top_k: int = 5
    score_threshold: float = 0.0
    max_context_chunks: int = 12


class OrchestrationLimits(BaseModel):
    """Agent orchestration guardrails (SDD A.5.2)."""

    max_iterations: int = 10
    per_step_timeout_seconds: int = 60


class RetryPolicy(BaseModel):
    """Retry-with-backoff policy for transient failures (SDD A.5.2)."""

    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0


class PromptSettings(BaseModel):
    """Where versioned prompt artifacts live and how strictly they load (AR-4)."""

    directory: str = "prompts"
    strict: bool = True


class Settings(BaseSettings):
    """Process configuration. Nested groups are populated with the ``__`` delimiter."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "Domain Copilot"
    environment: str = "development"
    api_v1_str: str = "/api/v1"

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    limits: OrchestrationLimits = Field(default_factory=OrchestrationLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    prompts: PromptSettings = Field(default_factory=PromptSettings)


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` instance."""
    return Settings()
    return Settings()
