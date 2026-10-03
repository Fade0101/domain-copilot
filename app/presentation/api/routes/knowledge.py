"""Authenticated, non-streaming retrieval and Q&A on the shared corpus."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.application.auth.context import Principal
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.domain.auth.value_objects import Permission
from app.presentation.api.dependencies import get_ask_use_case, get_hybrid_retrieval_use_case
from app.presentation.api.schemas.knowledge import (
    AskRequest,
    AskResponse,
    CitationResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from app.presentation.api.security import require_permission

router = APIRouter(tags=["knowledge"])


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    request: RetrieveRequest,
    principal: Principal = Depends(require_permission(Permission.ASK_QUESTION)),
    use_case: HybridRetrievalUseCase = Depends(get_hybrid_retrieval_use_case),
) -> RetrieveResponse:
    result = await use_case.execute(request.query, principal)
    return RetrieveResponse(
        trace_id=UUID(result.trace_id),
        citations=[CitationResponse.model_validate(citation) for citation in result.citations],
    )


@router.post("/ask", response_model=AskResponse)
async def ask(
    request: AskRequest,
    principal: Principal = Depends(require_permission(Permission.ASK_QUESTION)),
    use_case: AskUseCase = Depends(get_ask_use_case),
) -> AskResponse:
    return AskResponse.model_validate(await use_case.execute(request.question, principal))
