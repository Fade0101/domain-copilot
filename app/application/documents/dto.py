"""Output DTOs (read models) for the documents use cases.

A ``DocumentView`` is a serialization-friendly projection of the ``Document``
entity. Returning views (rather than entities) from use cases keeps domain
objects from leaking outward and gives the presentation layer a stable shape to
map onto its response schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.documents.entities import Document


@dataclass(frozen=True, slots=True)
class DocumentView:
    """Read model describing a registered document."""

    id: str
    filename: str
    content_hash: str
    status: str
    version: int
    registered_at: datetime
    metadata: dict[str, str]

    @classmethod
    def from_entity(cls, document: Document) -> DocumentView:
        """Project a domain :class:`Document` into a view."""
        return cls(
            id=document.id.value,
            filename=document.filename.value,
            content_hash=document.content_hash.value,
            status=document.status.value,
            version=document.version,
            registered_at=document.registered_at,
            metadata=dict(document.metadata),
        )
