"""Application configuration (BRD AR-4).

Typed settings loaded from the environment / ``.env`` via pydantic-settings.
Configuration is an *edge* concern, so pydantic is permitted here (and in the
presentation schemas) but nowhere in domain/application. Secrets are never
hard-coded: values come from the environment, keeping them out of code and git
history (constraint C6), and API keys use :class:`~pydantic.SecretStr` so they
are masked in logs and ``repr``.

Grouped settings are nested pydantic models populated with the ``__`` delimiter,
so e.g. ``LLM__MODEL`` sets ``settings.llm.model`` and ``RETRY__MAX_RETRIES``
sets ``settings.retry.max_retries``. Almost every field has a safe default so the
app boots without a ``.env`` in development; real providers/brokers/keys are
supplied by the environment in each deployment. The exceptions are the auth
signing secret and demo password, which have no defaults on purpose -- see
:class:`AuthSettings`. ``extra="ignore"`` lets the shared ``.env`` carry variables
owned by other tickets (database) without breaking here. Concrete provider
*adapters* that consume this config land in their own tickets (#7 providers,
#9 vector store, #20 queue); this ticket owns the config surface itself.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseModel):
    """Chat/completion provider selection and fallback (adapters finalized in #7)."""

    provider: str = "groq"
    # Capability-specific transient-failure fallback for chat (AR-2c): the
    # composition root wraps ``provider`` with this one in a FallbackLLMProvider.
    # Blank/None disables fallback (primary only); a value equal to ``provider``
    # is ignored. Selection stays config-driven -- no adapter is hard-coded.
    fallback: str | None = "ollama"
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
    # Provenance stamp written alongside every stored vector (#9). Bump this when
    # the embedding pipeline changes in a way that makes old vectors incomparable
    # to new ones even though the model name is unchanged (e.g. a different
    # normalisation or pooling strategy). The retrieval store keys embeddings by
    # (chunk, model, version), so bumping it lets a re-embedded corpus coexist
    # with the old one instead of overwriting it mid-reindex.
    version: str = "1"


class QueueSettings(BaseModel):
    """Celery/Redis configuration. URLs may carry credentials and are omitted from repr."""

    broker_url: str = Field(default="redis://localhost:6379/0", repr=False)
    # Compatibility with Ticket 3 configuration; the runner disables this backend.
    result_backend: str = Field(default="redis://localhost:6379/1", repr=False)
    default_queue: str = Field(default="default", min_length=1, max_length=100)
    visibility_timeout_seconds: int = Field(default=3600, gt=0)
    publish_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    max_payload_bytes: int = Field(default=65_536, gt=0)
    max_checkpoint_bytes: int = Field(default=1_048_576, gt=0)


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


class DatabaseSettings(BaseModel):
    """PostgreSQL connection configuration (BRD FR-8 persistence, SEC-1a).

    ``url`` has no default. Without it the application falls back to in-memory
    auth adapters, which is convenient for a dev server but means user identity
    and object ownership are lost on restart -- so the composition root warns in
    development and refuses to start in production.

    A plain ``postgresql://`` URL is accepted and rewritten to the async driver;
    see :func:`~app.infrastructure.persistence.database.normalize_database_url`.
    """

    url: str | None = Field(default=None, repr=False)
    echo: bool = False


class AuthSettings(BaseModel):
    """Authentication and RBAC configuration (BRD FR-8, AC-8.1).

    ``secret_key`` is the one field in this file with no usable default, and that
    is deliberate. A default signing secret would be a committed credential that
    every deployment which forgot to override it would silently share, so the
    composition root generates a throwaway per-process secret in development and
    *refuses to boot* in production when none is configured (constraint C6).

    ``demo_password`` likewise has no default: the demo accounts are seeded only
    when a password is supplied by the environment, so no usable credential is
    ever committed to source.
    """

    secret_key: SecretStr | None = None
    algorithm: str = "HS256"
    # Issuer/audience are checked on every token, so a token minted for another
    # service -- or by one -- is rejected here.
    issuer: str = "domain-copilot"
    audience: str = "domain-copilot-api"
    # One hour. Ticket #5 issues no refresh token, so a shorter lifetime would
    # mean re-authenticating mid-session; revocation-by-demotion does not wait
    # for expiry, because every request reloads the user's stored role.
    access_token_ttl_seconds: int = 3600
    bcrypt_rounds: int = 12
    seed_demo_accounts: bool = True
    demo_password: SecretStr | None = None


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
    auth: AuthSettings = Field(default_factory=AuthSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    def is_production(self) -> bool:
        """Return whether this process is configured as a production deployment.

        Gates the fail-closed checks around signing secrets and demo accounts.
        """
        return self.environment.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` instance."""
    return Settings()
