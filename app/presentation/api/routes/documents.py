"""Documents API routes.

The representative vertical slice: ``POST /api/v1/documents`` accepts a request,
maps it to a framework-free command, resolves the use case via ``Depends()`` ->
composition root, and maps the result back to an HTTP response. Idempotent
re-registration (same ``content_hash``) returns the existing document with 200;
a new registration returns 201.

Document ingestion is an admin capability in the BRD AC-8.2 matrix, so the route
is gated on ``Permission.INGEST_DOCUMENTS``: unauthenticated callers get 401,
authenticated analysts and reviewers get 403. Ticket #5 added that guard; before
it, this endpoint was open.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.application.auth.context import Principal
from app.application.documents.commands import RegisterDocumentCommand
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.domain.auth.value_objects import Permission
from app.presentation.api.dependencies import get_register_document_use_case
from app.presentation.api.schemas.documents import DocumentResponse, RegisterDocumentRequest
from app.presentation.api.security import require_permission

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a document's metadata",
    responses={
        401: {"description": "Missing, malformed, or expired token"},
        403: {"description": "Authenticated, but not permitted to ingest documents"},
    },
)
async def register_document(
    payload: RegisterDocumentRequest,
    response: Response,
    use_case: RegisterDocumentUseCase = Depends(get_register_document_use_case),
    # Resolves the principal and enforces the admin-only capability before the
    # handler body runs. Bound even though the body does not read it, so the
    # dependency -- and therefore the check -- cannot be dropped by accident.
    principal: Principal = Depends(require_permission(Permission.INGEST_DOCUMENTS)),
) -> DocumentResponse:
    result = await use_case.execute(
        RegisterDocumentCommand(
            filename=payload.filename,
            content_hash=payload.content_hash,
            metadata=payload.metadata,
        )
    )
    if not result.created:
        # Idempotent hit: the document already existed. 200 rather than 201.
        response.status_code = status.HTTP_200_OK
    return DocumentResponse.from_view(result.document)
