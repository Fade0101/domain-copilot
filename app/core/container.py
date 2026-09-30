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

from functools import lru_cache

from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.errors import ProviderConfigurationError
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.llm import ILLMProvider
from app.application.ports.prompts import IPromptProvider
from app.application.ports.repositories import IDocumentRepository
from app.application.ports.system import IClock, IIdGenerator
from app.core.config import Settings, get_settings
from app.infrastructure.embeddings.local_adapter import LocalEmbeddingAdapter
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.ollama_adapter import OllamaAdapter
from app.infrastructure.persistence.in_memory.document_repository import (
    InMemoryDocumentRepository,
)
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider
from app.infrastructure.system.clock import SystemClock
from app.infrastructure.system.identifiers import UuidGenerator

# Supported chat providers, keyed by the lowercase name used in configuration
# (settings.llm.provider / settings.llm.fallback). Adding a provider is a one-line
# change here -- selection stays config-driven, never hard-coded at the call site.
_SUPPORTED_LLM_PROVIDERS = ("groq", "ollama")


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

    def register_document_use_case(self) -> RegisterDocumentUseCase:
        return RegisterDocumentUseCase(
            repository=self._document_repository,
            clock=self._clock,
            id_generator=self._id_generator,
        )


@lru_cache
def get_container() -> Container:
    """Return the process-wide :class:`Container` singleton."""
    return Container(settings=get_settings())
