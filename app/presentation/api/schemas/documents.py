"""Request/response schemas for the documents API.

Pydantic models live at the presentation edge only. They validate HTTP input and
shape HTTP output; they are converted to/from framework-free application
commands and views so pydantic never crosses into domain/application.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.application.documents.dto import DocumentView
from app.application.ports.ingestion import IngestionDocument


class RegisterDocumentRequest(BaseModel):
    """Body for ``POST /api/v1/documents``."""

    filename: str = Field(min_length=1, max_length=255, examples=["antibiotics-guideline.pdf"])
    content_hash: str = Field(
        description="Hex-encoded SHA-256 digest of the document content (idempotency key).",
        examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    )
    metadata: dict[str, str] = Field(default_factory=dict)


class DocumentResponse(BaseModel):
    """Representation of a registered document returned by the API."""

    id: str
    filename: str
    content_hash: str
    status: str
    version: int
    registered_at: datetime
    metadata: dict[str, str]

    @classmethod
    def from_view(cls, view: DocumentView) -> DocumentResponse:
        """Build a response from an application read model."""
        return cls(
            id=view.id,
            filename=view.filename,
            content_hash=view.content_hash,
            status=view.status,
            version=view.version,
            registered_at=view.registered_at,
            metadata=view.metadata,
        )


class IngestionAcceptedResponse(BaseModel):
    document_id: UUID
    job_id: UUID
    state: str
    status_url: str
    document_url: str
    reused: bool


class IngestionStatusResponse(BaseModel):
    id: UUID
    filename: str
    content_hash: str
    media_type: str
    version: int
    status: str
    job_id: UUID
    stages: dict[str, Any]
    error_stage: str | None
    error_message: str | None
    created_at: datetime
    ingested_at: datetime | None
    chunk_count: int

    @classmethod
    def from_document(cls, document: IngestionDocument) -> IngestionStatusResponse:
        return cls(
            id=document.id,
            filename=document.filename,
            content_hash=document.content_hash,
            media_type=document.media_type,
            version=document.version,
            status=document.status.lower(),
            job_id=document.job_id,
            stages={
                key: {**value, "status": value["status"].lower()}
                for key, value in document.stages.items()
            },
            error_stage=document.error_stage,
            error_message=document.error_message,
            created_at=document.created_at,
            ingested_at=document.ingested_at,
            chunk_count=document.chunk_count,
        )
