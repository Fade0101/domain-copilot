"""FastAPI dependency providers (BRD AR-3).

Bridges FastAPI's ``Depends()`` to the composition root. Presentation code
depends on *use cases* built by the container from ports; it never imports or
instantiates an infrastructure adapter. This indirection is what the
``presentation -> infrastructure`` import ban and the AST boundary test protect,
and what lets the integration test resolve the real adapter through DI.
"""

from __future__ import annotations

from app.application.approvals.service import ApprovalService
from app.application.auth.use_cases import (
    AuthenticateUserUseCase,
    ResolvePrincipalUseCase,
)
from app.application.documents.ingestion_service import IngestionService
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.evaluation.service import EvaluationService
from app.application.jobs.service import JobService
from app.application.observability.cost_service import CostService
from app.application.observability.health_service import HealthService
from app.application.observability.trace_service import TraceService
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.llm import ILLMProvider
from app.application.ports.prompts import IPromptProvider
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.application.workflow.service import ClinicalWorkflowService
from app.core.container import Container, get_container


def get_app_container() -> Container:
    """Provide the process-wide composition root."""
    return get_container()


def get_trace_service() -> TraceService:
    return get_container().trace_service()


def get_cost_service() -> CostService:
    return get_container().cost_service()


def get_health_service() -> HealthService:
    return get_container().health_service()


async def get_approval_service() -> ApprovalService:
    return get_container().approval_service()


async def get_job_service() -> JobService:
    """Resolve on the API event loop so lazy runtime construction is serialized."""
    return get_container().job_service


async def get_ingestion_service() -> IngestionService:
    return get_container().ingestion_service


async def get_evaluation_service() -> EvaluationService:
    return get_container().evaluation_service


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


async def get_hybrid_retrieval_use_case() -> HybridRetrievalUseCase:
    return get_container().hybrid_retrieval_use_case()


async def get_ask_use_case() -> AskUseCase:
    return get_container().ask_use_case()


async def get_workflow_service() -> ClinicalWorkflowService:
    """Provide the clinical workflow service built by the container (Ticket #17)."""
    return get_container().workflow_service
