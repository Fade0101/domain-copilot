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

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.documents.commands import RegisterDocumentCommand
from app.application.documents.ingestion_service import IngestionService, source_media_type
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.errors import ResourceNotFoundError, UploadTooLargeError
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.shared.errors import InvariantViolationError
from app.presentation.api.dependencies import get_ingestion_service, get_register_document_use_case
from app.presentation.api.schemas.documents import (
    DocumentResponse,
    IngestionAcceptedResponse,
    IngestionStatusResponse,
    RegisterDocumentRequest,
)
from app.presentation.api.security import (
    get_authorization_service,
    get_current_principal,
    require_permission,
)

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


@router.post(
    "/ingest",
    status_code=202,
    response_model=IngestionAcceptedResponse,
    summary="Upload a PDF or Markdown source for durable ingestion",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                kind: {"schema": {"type": "string", "format": "binary"}}
                for kind in ("application/pdf", "text/markdown")
            },
        }
    },
)
async def ingest_document(
    request: Request,
    response: Response,
    filename: str = Query(min_length=1, max_length=255),
    version: int = Query(default=1, ge=1, le=2_147_483_647),
    principal: Principal = Depends(require_permission(Permission.INGEST_DOCUMENTS)),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionAcceptedResponse:
    media_type = source_media_type(filename, request.headers.get("content-type", ""))
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise InvariantViolationError("Invalid Content-Length header.") from None
        if declared < 0:
            raise InvariantViolationError("Invalid Content-Length header.")
        if declared > service.max_upload_bytes:
            raise UploadTooLargeError("The source file exceeds the upload limit.")
    # Raw binary upload keeps the bound effective before multipart spooling, and
    # also covers chunked transfer with no Content-Length. No client path is opened.
    source = bytearray()
    async for block in request.stream():
        if len(source) + len(block) > service.max_upload_bytes:
            raise UploadTooLargeError("The source file exceeds the upload limit.")
        source.extend(block)
    accepted = await service.submit(
        filename,
        bytes(source),
        user_id=UUID(principal.user_id.value),
        media_type=media_type,
        version=version,
    )
    status_url = request.url_for("read_job", job_id=str(accepted.job.id)).path
    document_url = request.url_for(
        "read_ingested_document", document_id=str(accepted.document.id)
    ).path
    response.headers["Location"] = status_url
    return IngestionAcceptedResponse(
        document_id=accepted.document.id,
        job_id=accepted.job.id,
        state=accepted.job.state.value,
        status_url=status_url,
        document_url=document_url,
        reused=accepted.reused,
    )


@router.get("/{document_id}", response_model=IngestionStatusResponse)
async def read_ingested_document(
    document_id: str,
    principal: Principal = Depends(get_current_principal),
    authorization: AuthorizationService = Depends(get_authorization_service),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionStatusResponse:
    await authorization.require_resource_access(principal, ResourceType.DOCUMENT, document_id)
    try:
        identifier = UUID(document_id)
    except ValueError:
        raise ResourceNotFoundError("Ingested document not found.") from None
    return IngestionStatusResponse.from_document(await service.store.get(identifier))
