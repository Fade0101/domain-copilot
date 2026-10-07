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
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    base_url: str | None = None
    ollama_base_url: str = "http://localhost:11434"


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
    """Hybrid retrieval controls; scores are sigmoid-normalized reranker logits."""

    top_k: int = Field(default=5, gt=0, le=20)
    candidate_k: int = Field(default=20, gt=0, le=100)
    rrf_k: int = Field(default=60, gt=0, le=1000)
    score_threshold: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    max_context_chunks: int = Field(default=12, gt=0, le=20)
    max_context_characters: int = Field(default=24_000, gt=0, le=100_000)
    timeout_seconds: float = Field(default=180, gt=0, le=600, allow_inf_nan=False)


class RerankerSettings(BaseModel):
    """Local BGE execution controls. Model identity/revision are pinned in the adapter."""

    device: Literal["cpu", "cuda"] = "cpu"
    batch_size: int = Field(default=4, gt=0, le=32)
    max_length: int = Field(default=1024, ge=128, le=8192)
    cache_directory: str | None = None


class IngestionSettings(BaseModel):
    """Bounded PDF/Markdown ingestion with reproducible token windows."""

    chunk_tokens: int = Field(default=512, ge=16, le=4096)
    chunk_overlap: int = Field(default=64, ge=0)
    max_upload_bytes: int = Field(default=10_485_760, gt=0, le=104_857_600)
    max_pages: int = Field(default=500, gt=0)
    max_characters: int = Field(default=2_000_000, gt=0)
    max_chunks: int = Field(default=4096, gt=0)
    stage_timeout_seconds: float = Field(default=600, gt=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> IngestionSettings:
        if self.chunk_overlap >= self.chunk_tokens:
            raise ValueError("ingestion.chunk_overlap must be smaller than chunk_tokens")
        return self


class EvaluationSettings(BaseModel):
    """Server-owned versioned artifacts; never accept filesystem paths from an API caller."""

    dataset_path: str = "data/evaluation/golden.v2.json"
    corpus_manifest: str = "data/corpus/manifest.json"


class ApiSettings(BaseModel):
    """HTTP edge limits applied before a handler or validator sees a request (#26).

    Ingestion already bounds uploaded bytes while streaming them
    (``ingestion.max_upload_bytes``), but every other endpoint takes a JSON body
    that Starlette buffers in memory before pydantic validates it. A field-level
    ``max_length`` therefore cannot stop an oversized body -- the memory is already
    spent by the time the constraint is evaluated. ``max_request_bytes`` is the
    outer bound that can.

    The default is deliberately well above the largest legitimate JSON body (an
    edited clinical note plus a rejection reason is a few tens of kilobytes) and
    well below ``ingestion.max_upload_bytes``, so document uploads keep their own
    larger, streamed budget.
    """

    max_request_bytes: int = Field(default=1_048_576, gt=0, le=104_857_600)


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


class ModelRateSettings(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=255)
    prompt_per_million: float = Field(ge=0, allow_inf_nan=False)
    completion_per_million: float = Field(ge=0, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=255)


class ObservabilitySettings(BaseModel):
    readiness_timeout_seconds: float = Field(default=3, gt=0, le=30, allow_inf_nan=False)
    # Explicit deployment rates, including their source/version. Unknown is not free.
    rates: list[ModelRateSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_rates(self) -> ObservabilitySettings:
        keys = [(rate.provider, rate.model) for rate in self.rates]
        if len(set(keys)) != len(keys) or any(not rate.source.strip() for rate in self.rates):
            raise ValueError("Rates must be unique per provider/model and include a source.")
        return self


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
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    limits: OrchestrationLimits = Field(default_factory=OrchestrationLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    def is_production(self) -> bool:
        """Return whether this process is configured as a production deployment.

        Gates the fail-closed checks around signing secrets and demo accounts.
        """
        return self.environment.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` instance."""
    return Settings()
