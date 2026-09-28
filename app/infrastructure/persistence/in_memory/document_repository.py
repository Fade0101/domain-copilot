"""In-memory document repository adapter.

Interim persistence adapter implementing :class:`IDocumentRepository` with plain
dicts. Used for local runs and tests until the SQLAlchemy/pgvector adapter lands
(ticket #6). Not durable and not thread/process safe -- intended for a single
in-process dev server and for the unit/integration test suite.
"""

from __future__ import annotations

from app.application.ports.repositories import IDocumentRepository
from app.domain.documents.entities import Document
from app.domain.documents.value_objects import ContentHash, DocumentId


class InMemoryDocumentRepository(IDocumentRepository):
    """Dict-backed implementation of the document repository port."""

    def __init__(self) -> None:
        self._by_id: dict[str, Document] = {}
        self._id_by_hash: dict[str, str] = {}

    async def add(self, document: Document) -> None:
        self._by_id[document.id.value] = document
        self._id_by_hash[document.content_hash.value] = document.id.value

    async def get_by_id(self, document_id: DocumentId) -> Document | None:
        return self._by_id.get(document_id.value)

    async def get_by_content_hash(self, content_hash: ContentHash) -> Document | None:
        document_id = self._id_by_hash.get(content_hash.value)
        if document_id is None:
            return None
        return self._by_id.get(document_id)
