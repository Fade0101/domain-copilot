"""Request/response schemas for the documents API.

Pydantic models live at the presentation edge only. They validate HTTP input and
shape HTTP output; they are converted to/from framework-free application
commands and views so pydantic never crosses into domain/application.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.application.documents.dto import DocumentView


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
