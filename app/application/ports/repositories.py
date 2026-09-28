"""Persistence ports.

Interfaces (Ports) that the application layer depends on for durable storage.
Concrete adapters (in-memory now; SQLAlchemy/pgvector later) live in
``app.infrastructure`` and implement these Protocols. Keeping them as
``typing.Protocol`` means adapters conform structurally and the application
never imports an ORM.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.documents.entities import Document
from app.domain.documents.value_objects import ContentHash, DocumentId


class IDocumentRepository(Protocol):
    """Durable store for :class:`Document` aggregates.

    Methods are asynchronous to match the eventual async database adapter; the
    in-memory adapter implements them trivially.
    """

    async def add(self, document: Document) -> None:
        """Persist a new document. The caller guarantees id/content-hash uniqueness."""
        ...

    async def get_by_id(self, document_id: DocumentId) -> Document | None:
        """Return the document with ``document_id``, or ``None`` if absent."""
        ...

    async def get_by_content_hash(self, content_hash: ContentHash) -> Document | None:
        """Return the document whose content hash matches, or ``None`` (idempotency lookup)."""
        ...
