"""FastAPI dependency providers (BRD AR-3).

Bridges FastAPI's ``Depends()`` to the composition root. Presentation code
depends on *use cases* built by the container from ports; it never imports or
instantiates an infrastructure adapter. This indirection is what the
``presentation -> infrastructure`` import ban and the AST boundary test protect,
and what lets the integration test resolve the real adapter through DI.
"""

from __future__ import annotations

from app.application.auth.use_cases import (
    AuthenticateUserUseCase,
    ResolvePrincipalUseCase,
)
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.jobs.service import JobService
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.llm import ILLMProvider
from app.application.ports.prompts import IPromptProvider
from app.core.container import Container, get_container


def get_app_container() -> Container:
    """Provide the process-wide composition root."""
    return get_container()


async def get_job_service() -> JobService:
    """Resolve on the API event loop so lazy runtime construction is serialized."""
    return get_container().job_service


def get_register_document_use_case() -> RegisterDocumentUseCase:
    """Provide the RegisterDocument use case wired to its ports by the container."""
    return get_container().register_document_use_case()


def get_authenticate_user_use_case() -> AuthenticateUserUseCase:
    """Provide the AuthenticateUser use case (login) built by the container (FR-8)."""
    return get_container().authenticate_user_use_case()


def get_resolve_principal_use_case() -> ResolvePrincipalUseCase:
    """Provide the ResolvePrincipal use case used on every protected request (FR-8)."""
    return get_container().resolve_principal_use_case()


def get_prompt_provider() -> IPromptProvider:
    """Provide the versioned prompt provider built by the container (AR-4)."""
    return get_container().prompt_provider


def get_llm_provider() -> ILLMProvider:
    """Provide the canonical LLM provider built by the container (AR-2a)."""
    return get_container().llm_provider


def get_embedding_provider() -> IEmbeddingProvider:
    """Provide the embedding provider built by the container (AR-2b)."""
    return get_container().embedding_provider
