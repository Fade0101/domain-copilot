"""Private conversation metadata and history; no client-authored assistant evidence."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.application.auth.context import Principal
from app.application.sessions import SessionService
from app.presentation.api.dependencies import get_session_service
from app.presentation.api.schemas.sessions import (
    CreateSessionRequest,
    MessageListResponse,
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)
from app.presentation.api.security import get_current_principal

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201, response_model=SessionResponse)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: SessionService = Depends(get_session_service),
) -> SessionResponse:
    """Every role may create its own session. Ownership comes from stored identity."""
    session = await service.create(body.title, principal)
    response.headers["Location"] = request.url_for("read_session", session_id=str(session.id)).path
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse.model_validate(session)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    service: SessionService = Depends(get_session_service),
) -> SessionListResponse:
    """Own sessions only, including for admin. Newest created_at then descending id."""
    sessions = await service.list_sessions(principal, limit=limit, offset=offset)
    response.headers["Cache-Control"] = "no-store"
    return SessionListResponse(
        items=[SessionResponse.model_validate(item) for item in sessions],
        limit=limit,
        offset=offset,
    )


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def session_messages(
    session_id: UUID,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    service: SessionService = Depends(get_session_service),
) -> MessageListResponse:
    """Owner only for all roles. Messages ordered by committed sequence, oldest first.

    POST /ask with session_id appends an atomic user/assistant pair, including refusals.
    History is never promoted to clinical evidence or fed back as trusted model instructions.
    """
    messages = await service.messages(session_id, principal, limit=limit, offset=offset)
    response.headers["Cache-Control"] = "no-store"
    return MessageListResponse(
        items=[MessageResponse.model_validate(item) for item in messages],
        limit=limit,
        offset=offset,
    )
