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

import logging
import secrets
from functools import lru_cache

from app.application.auth.authorization import AuthorizationService
from app.application.auth.seeding import (
    SeedDemoAccountsResult,
    SeedDemoAccountsUseCase,
)
from app.application.auth.use_cases import (
    AuthenticateUserUseCase,
    ResolvePrincipalUseCase,
)
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.errors import ConfigurationError, ProviderConfigurationError
from app.application.ports.audit import IAuditSink
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.llm import ILLMProvider
from app.application.ports.ownership import IOwnershipQuery
from app.application.ports.passwords import IPasswordHasher
from app.application.ports.prompts import IPromptProvider
from app.application.ports.repositories import IDocumentRepository, IUserRepository
from app.application.ports.system import IClock, IIdGenerator
from app.application.ports.tokens import ITokenService
from app.core.config import Settings, get_settings
from app.domain.auth.value_objects import Password
from app.infrastructure.audit.logging_sink import LoggingAuditSink
from app.infrastructure.auth.password_hasher import BcryptPasswordHasher
from app.infrastructure.auth.token_service import JwtTokenService
from app.infrastructure.embeddings.local_adapter import LocalEmbeddingAdapter
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
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider
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
            base_url="http://localhost:11434",
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

    async def dispose(self) -> None:
        """Release process-wide resources. Called from the application lifespan."""
        if self._database is not None:
            await self._database.dispose()

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
