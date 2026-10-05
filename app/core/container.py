"""Composition root (BRD AR-3).

The single place where concrete adapters are constructed and wired to the ports
that use cases depend on. This module -- and only this module -- is permitted to
import from ``app.infrastructure`` and to know concrete adapter classes.

Presentation asks the container for a *use case*; it never instantiates an
adapter. Swapping an adapter (e.g. in-memory -> SQLAlchemy) is a one-line change
here, which is the whole point of the dependency rules (BRD AR-1).

Note on direction: ``core`` importing ``domain`` + ``application`` +
``infrastructure`` is intentional and correct -- the composition root sits at
the outermost point of the dependency graph. Infrastructure must NOT be made to
depend on ``core`` to "avoid" this import.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from celery import Celery
from sqlalchemy.engine import Engine

from app.application.agents.guideline_researcher import GuidelineResearcherAgent
from app.application.agents.safety_checker import SafetyCheckerAgent
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.auth.seeding import (
    SeedDemoAccountsResult,
    SeedDemoAccountsUseCase,
)
from app.application.auth.use_cases import (
    AuthenticateUserUseCase,
    ResolvePrincipalUseCase,
)
from app.application.clinical_tools.execution import ClinicalToolFactory
from app.application.documents.chunking import StructureAwareChunker
from app.application.documents.embedding import IngestionEmbedder
from app.application.documents.ingestion_handler import DocumentIngestionHandler
from app.application.documents.ingestion_service import IngestionService
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.errors import ConfigurationError, ProviderConfigurationError
from app.application.evaluation.service import EvaluationService
from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.application.ports.audit import IAuditSink
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.jobs import IJobHandler, IJobStore
from app.application.ports.llm import ILLMProvider
from app.application.ports.ownership import IOwnershipQuery
from app.application.ports.passwords import IPasswordHasher
from app.application.ports.prompts import IPromptProvider
from app.application.ports.queue import IJobQueue
from app.application.ports.repositories import IDocumentRepository, IUserRepository
from app.application.ports.reranking import IReranker
from app.application.ports.retrieval import IRetrievalStore
from app.application.ports.system import IClock, IIdGenerator
from app.application.ports.tokens import ITokenService
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.core.clinical_tools import build_clinical_tool_factory
from app.core.config import Settings, get_settings
from app.core.evaluation import EvaluationComponents, build_evaluation_components
from app.core.knowledge import build_hybrid_retrieval
from app.domain.auth.value_objects import Password
from app.domain.documents.ingestion import IngestionOptions
from app.infrastructure.audit.logging_sink import LoggingAuditSink
from app.infrastructure.audit.retrieval_sink import PostgresRetrievalAuditSink
from app.infrastructure.auth.password_hasher import BcryptPasswordHasher
from app.infrastructure.auth.token_service import JwtTokenService
from app.infrastructure.embeddings.local_adapter import LocalEmbeddingAdapter
from app.infrastructure.ingestion.extractors import DocumentExtractor
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.ollama_adapter import OllamaAdapter
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.in_memory.document_repository import (
    InMemoryDocumentRepository,
)
from app.infrastructure.persistence.in_memory.ownership_query import (
    InMemoryOwnershipQuery,
)
from app.infrastructure.persistence.in_memory.user_repository import (
    InMemoryUserRepository,
)
from app.infrastructure.persistence.job_store import PostgresJobStore, create_job_engine
from app.infrastructure.persistence.sql.ingestion_store import PostgresIngestionStore
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider
from app.infrastructure.queue.celery_queue import (
    CeleryJobQueue,
    create_celery_app,
    register_job_task,
)
from app.infrastructure.reranking.local_adapter import LocalCrossEncoderReranker
from app.infrastructure.system.clock import SystemClock
from app.infrastructure.system.identifiers import UuidGenerator

logger = logging.getLogger("app.core.container")

# Supported chat providers, keyed by the lowercase name used in configuration
# (settings.llm.provider / settings.llm.fallback). Adding a provider is a one-line
# change here -- selection stays config-driven, never hard-coded at the call site.
_SUPPORTED_LLM_PROVIDERS = ("groq", "ollama")

#: Bytes of entropy in a generated development signing secret.
_EPHEMERAL_SECRET_BYTES = 48


def _build_llm_adapter(name: str, settings: Settings) -> ILLMProvider:
    """Construct a single chat adapter by its configured name.

    An unknown name is a configuration fault, not a client error, so it raises the
    typed :class:`ProviderConfigurationError` (a non-transient server fault) rather
    than falling through -- the app fails to boot instead of silently selecting the
    wrong provider.
    """
    key = name.strip().lower()
    if key == "groq":
        api_key = settings.llm.api_key.get_secret_value() if settings.llm.api_key else ""
        return GroqAdapter(api_key=api_key, default_model=settings.llm.model)
    if key == "ollama":
        return OllamaAdapter(
            base_url=settings.llm.ollama_base_url,
            default_model=settings.llm.model,
        )
    raise ProviderConfigurationError(
        f"Unknown LLM provider {name!r}; supported providers are: "
        f"{', '.join(_SUPPORTED_LLM_PROVIDERS)}."
    )


def build_llm_provider(settings: Settings) -> ILLMProvider:
    """Build the chat provider from configuration (BRD AR-2, ADR-007).

    ``settings.llm.provider`` selects the primary adapter and ``settings.llm.fallback``
    the transient-failure fallback. When a distinct fallback is configured the two are
    composed in a :class:`FallbackLLMProvider` (primary first, fallback on transient
    error only); a blank/None fallback -- or one equal to the primary -- yields the
    bare primary adapter. Both names are validated, so invalid config fails safely.
    """
    primary = _build_llm_adapter(settings.llm.provider, settings)
    fallback_name = (settings.llm.fallback or "").strip().lower()
    if not fallback_name or fallback_name == settings.llm.provider.strip().lower():
        return primary
    secondary = _build_llm_adapter(fallback_name, settings)
    return FallbackLLMProvider(primary=primary, secondary=secondary)


def build_jwt_secret(settings: Settings) -> str:
    """Resolve the JWT signing secret, or fail closed (BRD AC-8.1, constraint C6).

    There is no default secret to fall back on, which leaves three cases:

    * **Configured.** Use it.
    * **Absent in production.** Raise :class:`ConfigurationError`. The app does not
      start. A production deployment signing tokens with a value an attacker could
      read out of a public repository -- or one that silently rotates on restart --
      is worse than a deployment that refuses to boot and says why.
    * **Absent in development.** Generate a random secret for this process and warn.
      Tokens stay valid for the life of the process and are invalidated by a
      restart, which is the right trade for a dev server: nothing is committed,
      nothing is shared between machines, and no developer has to invent a secret
      before the app will run.
    """
    configured = settings.auth.secret_key
    if configured is not None:
        secret = configured.get_secret_value().strip()
        if secret:
            return secret

    if settings.is_production():
        raise ConfigurationError(
            "AUTH__SECRET_KEY must be set when ENVIRONMENT=production; "
            "refusing to start without a configured JWT signing secret."
        )

    logger.warning(
        "AUTH__SECRET_KEY is not configured; generating an ephemeral signing secret "
        "for this process. Tokens will not survive a restart. Set AUTH__SECRET_KEY "
        "for a stable development secret."
    )
    return secrets.token_urlsafe(_EPHEMERAL_SECRET_BYTES)


def build_database(settings: Settings) -> Database | None:
    """Build the database connection, or ``None`` to run on in-memory adapters.

    Returning ``None`` is a development convenience: the app boots and the auth
    flows work without PostgreSQL running. It is not safe beyond that, because
    user identity and object ownership then live in process memory and vanish on
    restart -- so production refuses to start without ``DATABASE__URL`` rather than
    silently serving authorization decisions from a store that forgets.
    """
    url = (settings.database.url or "").strip()
    if not url:
        if settings.is_production():
            raise ConfigurationError(
                "DATABASE__URL must be set when ENVIRONMENT=production; refusing to "
                "start with in-memory user identity and object ownership."
            )
        logger.warning(
            "DATABASE__URL is not configured; user identity and object ownership will "
            "be held in memory and lost on restart. Set DATABASE__URL to persist them."
        )
        return None
    return Database(url, echo=settings.database.echo)


class Container:
    """Holds process-wide singletons and builds use cases via constructor injection.

    Adapter attributes are annotated with their *port* types, so mypy verifies at
    this wiring site that each concrete adapter structurally satisfies its port.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._document_repository: IDocumentRepository = InMemoryDocumentRepository()
        self._clock: IClock = SystemClock()
        self._id_generator: IIdGenerator = UuidGenerator()
        # Eagerly loads and validates prompts/*.yaml when strict -- a malformed
        # prompt fails app startup here rather than on first request.
        self._prompt_provider: IPromptProvider = YamlPromptProvider(
            settings.prompts.directory, strict=settings.prompts.strict
        )

        # Chat provider (primary + optional transient-failure fallback) is chosen
        # from configuration here -- the single composition root -- never hard-coded.
        self._llm_provider: ILLMProvider = build_llm_provider(settings)

        self._embedding_provider: IEmbeddingProvider = LocalEmbeddingAdapter(
            model_name=settings.embedding.model
        )

        # --- Authentication & RBAC (Ticket #5) ------------------------------
        # PostgreSQL when configured, in-memory otherwise. Both satisfy the same
        # ports, so nothing above this line changes; what changes is whether an
        # authorization decision survives a restart.
        self._database = build_database(settings)
        self._user_repository: IUserRepository
        self._ownership_query: IOwnershipQuery
        if self._database is None:
            self._user_repository = InMemoryUserRepository()
            self._ownership_query = InMemoryOwnershipQuery()
        else:
            self._user_repository = SqlUserRepository(self._database.session_factory)
            self._ownership_query = SqlOwnershipQuery(self._database.session_factory)
        self._password_hasher: IPasswordHasher = BcryptPasswordHasher(
            rounds=settings.auth.bcrypt_rounds
        )
        # Fails closed on a missing production secret -- app startup, not first login.
        self._token_service: ITokenService = JwtTokenService(
            secret=build_jwt_secret(settings),
            issuer=settings.auth.issuer,
            audience=settings.auth.audience,
            ttl_seconds=settings.auth.access_token_ttl_seconds,
            clock=self._clock,
            algorithm=settings.auth.algorithm,
        )
        self._audit_sink: IAuditSink = LoggingAuditSink()
        self._authorization_service = AuthorizationService(ownership_query=self._ownership_query)
        self._jobs: JobRuntime | None = None

        # --- Retrieval (Ticket #9) ------------------------------------------
        # Shares the one Database built above rather than opening a second pool.
        # There is no in-memory retrieval adapter: dense search needs pgvector and
        # keyword search needs PostgreSQL full-text search, so without a database
        # this is None and callers must say so rather than silently degrade to a
        # store that cannot answer. Embedding provenance comes from configuration
        # here, keeping provider knowledge in the composition root.
        self._retrieval_store: IRetrievalStore | None = (
            None
            if self._database is None
            else PostgresRetrievalStore(
                self._database.session_factory,
                embedding_model=settings.embedding.model,
                embedding_dim=settings.embedding.dimensions,
                embedding_version=settings.embedding.version,
            )
        )
        self._reranker = LocalCrossEncoderReranker(
            device=settings.reranker.device,
            batch_size=settings.reranker.batch_size,
            max_length=settings.reranker.max_length,
            cache_directory=settings.reranker.cache_directory,
        )
        retrieval_sink: IAuditSink = (
            self._audit_sink
            if self._database is None
            else PostgresRetrievalAuditSink(self._database.session_factory, self._audit_sink)
        )
        self._retrieval_observer = RetrievalObserver(retrieval_sink, self._clock)

    @property
    def llm_provider(self) -> ILLMProvider:
        return self._llm_provider

    @property
    def embedding_provider(self) -> IEmbeddingProvider:
        return self._embedding_provider

    @property
    def document_repository(self) -> IDocumentRepository:
        return self._document_repository

    @property
    def prompt_provider(self) -> IPromptProvider:
        return self._prompt_provider

    @property
    def user_repository(self) -> IUserRepository:
        return self._user_repository

    @property
    def ownership_query(self) -> IOwnershipQuery:
        return self._ownership_query

    @property
    def database(self) -> Database | None:
        """The database connection, or ``None`` when running on in-memory adapters."""
        return self._database

    @property
    def retrieval_store(self) -> IRetrievalStore | None:
        """The durable dense + keyword index, or ``None`` without a database.

        Unlike the auth ports there is no in-memory fallback: hybrid retrieval is
        defined in terms of pgvector and PostgreSQL full-text search, so a
        stand-in would answer queries it cannot actually serve.
        """
        return self._retrieval_store

    async def dispose(self) -> None:
        """Release process-wide resources. Called from the application lifespan."""
        self._reranker.close()
        if self._jobs is not None:
            await self._jobs.close()
            self._jobs = None
        if self._database is not None:
            await self._database.dispose()

    @property
    def reranker(self) -> IReranker:
        return self._reranker

    def hybrid_retrieval_use_case(self) -> HybridRetrievalUseCase:
        if self._retrieval_store is None:
            raise ConfigurationError("DATABASE__URL is required for hybrid retrieval.")
        return build_hybrid_retrieval(
            self.settings,
            self._retrieval_store,
            self._embedding_provider,
            self._reranker,
            self._authorization_service,
            self._retrieval_observer,
            self._id_generator,
        )

    def ask_use_case(self) -> AskUseCase:
        return AskUseCase(
            self.hybrid_retrieval_use_case(),
            self._llm_provider,
            self._prompt_provider,
            self._retrieval_observer,
            self._id_generator,
            max_tokens=self.settings.llm.max_tokens,
            timeout_seconds=self.settings.llm.timeout_seconds,
        )

    def clinical_tool_factory(self) -> ClinicalToolFactory:
        """Trusted agent wiring only; no user-selectable agent/role endpoint."""
        if self._database is None:
            raise ConfigurationError("DATABASE__URL is required for clinical tools.")
        return build_clinical_tool_factory(
            self._user_repository,
            self._authorization_service,
            self.hybrid_retrieval_use_case(),
            self.ask_use_case(),
            self._database.session_factory,
            self._retrieval_observer,
            self._clock,
            self._id_generator,
        )

    def guideline_researcher_agent(
        self, principal: Principal, workflow_id: UUID
    ) -> GuidelineResearcherAgent:
        """Build Guideline Researcher agent (AGT-01) bound to principal and workflow."""
        factory = self.clinical_tool_factory()
        tools = factory.for_guideline_researcher(principal, workflow_id)
        return GuidelineResearcherAgent(
            llm=self._llm_provider,
            tools=tools,
            prompts=self._prompt_provider,
            observer=self._retrieval_observer,
            timeout_seconds=self.settings.llm.timeout_seconds or 20.0,
        )

    def safety_checker_agent(self, principal: Principal, workflow_id: UUID) -> SafetyCheckerAgent:
        """Build Safety Checker agent (AGT-02) bound to principal and workflow."""
        factory = self.clinical_tool_factory()
        tools = factory.for_safety_checker(principal, workflow_id)
        return SafetyCheckerAgent(
            llm=self._llm_provider,
            tools=tools,
            prompts=self._prompt_provider,
            observer=self._retrieval_observer,
            timeout_seconds=self.settings.llm.timeout_seconds or 20.0,
        )

    @property
    def job_service(self) -> JobService:
        """Build the durable queue on first use; never substitute in-memory jobs."""
        if self._jobs is None:
            self._jobs = build_job_runtime(self.settings, database=self._database)
        return self._jobs.service

    @property
    def ingestion_service(self) -> IngestionService:
        self.job_service  # Resolve the shared runtime on the API event loop.
        if self._jobs is None or self._jobs.ingestion is None:
            raise ConfigurationError("DATABASE__URL is required for durable ingestion.")
        return self._jobs.ingestion

    @property
    def evaluation_service(self) -> EvaluationService:
        self.job_service
        if self._jobs is None or self._jobs.evaluation is None:
            raise ConfigurationError("DATABASE__URL is required for evaluation.")
        return self._jobs.evaluation

    @property
    def password_hasher(self) -> IPasswordHasher:
        return self._password_hasher

    @property
    def token_service(self) -> ITokenService:
        return self._token_service

    @property
    def audit_sink(self) -> IAuditSink:
        return self._audit_sink

    @property
    def authorization_service(self) -> AuthorizationService:
        return self._authorization_service

    def register_document_use_case(self) -> RegisterDocumentUseCase:
        return RegisterDocumentUseCase(
            repository=self._document_repository,
            clock=self._clock,
            id_generator=self._id_generator,
        )

    def authenticate_user_use_case(self) -> AuthenticateUserUseCase:
        return AuthenticateUserUseCase(
            users=self._user_repository,
            password_hasher=self._password_hasher,
            token_service=self._token_service,
            audit_sink=self._audit_sink,
            clock=self._clock,
        )

    def resolve_principal_use_case(self) -> ResolvePrincipalUseCase:
        return ResolvePrincipalUseCase(
            users=self._user_repository,
            token_service=self._token_service,
        )

    def seed_demo_accounts_use_case(self) -> SeedDemoAccountsUseCase:
        return SeedDemoAccountsUseCase(
            users=self._user_repository,
            password_hasher=self._password_hasher,
            clock=self._clock,
            id_generator=self._id_generator,
        )

    async def seed_demo_accounts(self) -> SeedDemoAccountsResult | None:
        """Seed the per-role demo accounts, or return ``None`` without doing so.

        Called from the API lifespan hook and from ``scripts/seed_demo_accounts.py``.
        It is a coroutine because the repository port is async, which is also why
        it cannot live in ``__init__``: a synchronous ``asyncio.run`` there would
        fail outright under an already-running event loop.

        Seeding is skipped -- never fatal -- in three cases, each logged:
        turned off by configuration, running in production, or no demo password
        supplied. The last is what keeps a guessable credential out of the
        repository: absent a configured password there is nothing to fall back to.
        """
        if not self.settings.auth.seed_demo_accounts:
            logger.info("demo account seeding is disabled (AUTH__SEED_DEMO_ACCOUNTS)")
            return None

        if self.settings.is_production():
            logger.warning("refusing to seed demo accounts with ENVIRONMENT=production")
            return None

        configured = self.settings.auth.demo_password
        raw_password = configured.get_secret_value() if configured else ""
        if not raw_password:
            logger.warning(
                "AUTH__DEMO_PASSWORD is not set; skipping demo account seeding. "
                "Set it to create the analyst/reviewer/admin development accounts."
            )
            return None

        result = await self.seed_demo_accounts_use_case().execute(Password(raw_password))
        # Emails and counts only. The password never reaches a log record.
        logger.info(
            "demo accounts seeded: created=%s skipped=%s",
            ",".join(result.created) or "-",
            ",".join(result.skipped) or "-",
        )
        return result


@lru_cache
def get_container() -> Container:
    """Return the process-wide :class:`Container` singleton."""
    return Container(settings=get_settings())


@dataclass
class JobRuntime:
    """Worker/API wiring, built without loading LLM clients or model weights."""

    service: JobService
    runner: JobRunner
    celery_app: Celery
    engine: Engine
    database: Database | None = None
    ingestion: IngestionService | None = None
    embeddings: LocalEmbeddingAdapter | None = None
    owns_database: bool = True
    evaluation: EvaluationService | None = None
    evaluation_components: EvaluationComponents | None = None

    async def close(self) -> None:
        if self.evaluation_components is not None:
            self.evaluation_components.reranker.close()
        self.celery_app.close()
        await asyncio.to_thread(self.engine.dispose)
        if self.database is not None and self.owns_database:
            await self.database.dispose()
        if self.embeddings is not None:
            self.embeddings.close()


def build_ingestion_options(settings: Settings) -> IngestionOptions:
    return IngestionOptions(
        chunk_tokens=settings.ingestion.chunk_tokens,
        chunk_overlap=settings.ingestion.chunk_overlap,
        max_chunks=settings.ingestion.max_chunks,
        embedding_batch_size=settings.embedding.batch_size,
        embedding_model=settings.embedding.model,
        embedding_dim=settings.embedding.dimensions,
        embedding_version=settings.embedding.version,
    )


def build_job_runtime(
    settings: Settings,
    *,
    handlers: Iterable[IJobHandler] | None = None,
    database: Database | None = None,
) -> JobRuntime:
    if not settings.database.url:
        raise ConfigurationError("DATABASE__URL is required for durable jobs.")
    engine = create_job_engine(settings.database.url)
    store: IJobStore = PostgresJobStore(engine)
    clock = SystemClock()
    owns_database = database is None
    embeddings = None
    ingestion_store = None
    evaluation_components = None
    if handlers is None:
        # API callers borrow the existing Database; workers use the same engine
        # factory without pooling across Celery's per-task asyncio.run event loops.
        database = database or Database(settings.database.url, pooling=False)
        embeddings = LocalEmbeddingAdapter(model_name=settings.embedding.model)
        ingestion_store = PostgresIngestionStore(database.session_factory)
        options = build_ingestion_options(settings)
        retrieval = PostgresRetrievalStore(
            database.session_factory,
            embedding_model=options.embedding_model,
            embedding_dim=options.embedding_dim,
            embedding_version=options.embedding_version,
        )
        evaluation_components = build_evaluation_components(
            settings,
            database,
            embeddings,
            retrieval,
            store,
            clock,
            lambda: build_llm_provider(settings),
        )
        handlers = [
            DiagnosticJobHandler(),
            evaluation_components.handler,
            DocumentIngestionHandler(
                ingestion_store,
                DocumentExtractor(
                    max_pages=settings.ingestion.max_pages,
                    max_characters=settings.ingestion.max_characters,
                ),
                StructureAwareChunker(embeddings),
                IngestionEmbedder(embeddings, embeddings),
                retrieval,
                clock,
                options,
                stage_timeout_seconds=settings.ingestion.stage_timeout_seconds,
            ),
        ]
    registry = JobHandlerRegistry(handlers)
    celery_app = create_celery_app(
        settings.queue.broker_url,
        settings.queue.default_queue,
        visibility_timeout=settings.queue.visibility_timeout_seconds,
        connection_timeout=settings.queue.publish_timeout_seconds,
    )
    queue: IJobQueue = CeleryJobQueue(celery_app)
    runner = JobRunner(
        store,
        registry,
        clock,
        max_checkpoint_bytes=settings.queue.max_checkpoint_bytes,
    )
    service = JobService(
        store,
        queue,
        registry,
        clock,
        UuidGenerator(),
        max_payload_bytes=settings.queue.max_payload_bytes,
    )
    register_job_task(celery_app, runner)
    ingestion = (
        None
        if ingestion_store is None
        else IngestionService(
            ingestion_store,
            service,
            build_ingestion_options(settings),
            max_upload_bytes=settings.ingestion.max_upload_bytes,
        )
    )
    evaluation = (
        None
        if evaluation_components is None
        else EvaluationService(
            service,
            evaluation_components.catalog,
            evaluation_components.artifacts,
            evaluation_components.authorization,
            clock,
        )
    )
    return JobRuntime(
        service,
        runner,
        celery_app,
        engine,
        database,
        ingestion,
        embeddings,
        owns_database,
        evaluation,
        evaluation_components,
    )


@lru_cache
def get_job_runtime() -> JobRuntime:
    return build_job_runtime(get_settings())
