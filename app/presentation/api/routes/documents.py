"""Documents API routes.

The representative vertical slice: ``POST /api/v1/documents`` accepts a request,
maps it to a framework-free command, resolves the use case via ``Depends()`` ->
composition root, and maps the result back to an HTTP response. Idempotent
re-registration (same ``content_hash``) returns the existing document with 200;
a new registration returns 201.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.application.documents.commands import RegisterDocumentCommand
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.presentation.api.dependencies import get_register_document_use_case
from app.presentation.api.schemas.documents import DocumentResponse, RegisterDocumentRequest

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a document's metadata",
)
async def register_document(
    payload: RegisterDocumentRequest,
    response: Response,
    use_case: RegisterDocumentUseCase = Depends(get_register_document_use_case),
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
