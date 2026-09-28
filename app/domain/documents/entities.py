"""The Document aggregate root.

Encapsulates a document's identity, metadata, and ingestion-readiness
lifecycle. Every state change goes through a method that enforces the allowed
transitions; callers never mutate ``status`` directly. The entity is immutable
-- lifecycle operations return a new ``Document`` -- which keeps it trivial to
reason about and to test.

Pure stdlib + domain value objects and errors only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from app.domain.documents.value_objects import (
    ContentHash,
    DocumentId,
    DocumentStatus,
    Filename,
)
from app.domain.shared.errors import InvalidStateTransitionError

# Allowed status transitions. A target absent from a state's set is forbidden.
_ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.REGISTERED: frozenset({DocumentStatus.INGESTING}),
    DocumentStatus.INGESTING: frozenset({DocumentStatus.INGESTED, DocumentStatus.FAILED}),
    DocumentStatus.INGESTED: frozenset(),
    DocumentStatus.FAILED: frozenset({DocumentStatus.INGESTING}),
}


@dataclass(frozen=True, slots=True)
class Document:
    """A registered corpus document.

    Immutable: lifecycle methods return a new instance via ``dataclasses.replace``
    rather than mutating in place.
    """

    id: DocumentId
    filename: Filename
    content_hash: ContentHash
    status: DocumentStatus
    version: int
    registered_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def register(
        cls,
        *,
        document_id: DocumentId,
        filename: Filename,
        content_hash: ContentHash,
        registered_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Document:
        """Create a newly registered document (status ``REGISTERED``, version 1)."""
        return cls(
            id=document_id,
            filename=filename,
            content_hash=content_hash,
            status=DocumentStatus.REGISTERED,
            version=1,
            registered_at=registered_at,
            metadata=dict(metadata or {}),
        )

    def _with_status(self, target: DocumentStatus) -> Document:
        if target not in _ALLOWED_TRANSITIONS.get(self.status, frozenset()):
            raise InvalidStateTransitionError(
                f"Cannot transition document {self.id} from {self.status.value} to {target.value}"
            )
        return replace(self, status=target)

    def start_ingestion(self) -> Document:
        """Begin ingestion: ``REGISTERED`` or ``FAILED`` -> ``INGESTING``."""
        return self._with_status(DocumentStatus.INGESTING)

    def mark_ingested(self) -> Document:
        """Ingestion succeeded: ``INGESTING`` -> ``INGESTED``."""
        return self._with_status(DocumentStatus.INGESTED)

    def mark_failed(self) -> Document:
        """Ingestion failed: ``INGESTING`` -> ``FAILED`` (retryable)."""
        return self._with_status(DocumentStatus.FAILED)
