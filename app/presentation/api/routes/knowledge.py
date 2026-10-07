"""Authenticated retrieval and grounded Q&A on the shared corpus."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from app.application.auth.context import Principal
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.application.sessions import SessionService
from app.domain.auth.value_objects import Permission
from app.presentation.api.ask_stream import stream_answer
from app.presentation.api.dependencies import (
    get_ask_use_case,
    get_hybrid_retrieval_use_case,
    get_session_service,
)
from app.presentation.api.schemas.knowledge import (
    AnswerResponse,
    AskOutcome,
    AskRequest,
    CitationResponse,
    RefusalResponse,
    RetrieveRequest,
    RetrieveResponse,
    ask_outcome,
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


@router.post("/ask", response_model=AskOutcome)
async def ask(
    request: AskRequest,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.ASK_QUESTION)),
    use_case: AskUseCase = Depends(get_ask_use_case),
    sessions: SessionService = Depends(get_session_service),
) -> AnswerResponse | RefusalResponse | StreamingResponse:
    """All roles may ask. Optional session_id must belong to the caller, including admins.

    stream=false returns JSON; stream=true returns SSE after grounding completes.
    A refusal is HTTP 200 with refused=true, the exact refusal text and empty citations.
    This informational path never drafts, approves or finalizes a clinical note.
    """
    result = ask_outcome.validate_python(
        await sessions.ask(request.question, principal, request.session_id, use_case)
    )
    if request.stream:
        return StreamingResponse(
            stream_answer(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )
    response.headers["Cache-Control"] = "no-store"
    return result
