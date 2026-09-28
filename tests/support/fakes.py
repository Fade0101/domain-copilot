"""Test doubles implementing the application ports.

These fakes let the application layer be tested with no framework, database, or
LLM present (BRD AR-8). They implement the same Protocols the real adapters do.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.application.ports.repositories import IDocumentRepository
from app.application.ports.system import IClock, IIdGenerator
from app.domain.documents.entities import Document
from app.domain.documents.value_objects import ContentHash, DocumentId


class FakeDocumentRepository(IDocumentRepository):
    """In-test document store exposing its contents for assertions."""

    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}

    async def add(self, document: Document) -> None:
        self.documents[document.id.value] = document

    async def get_by_id(self, document_id: DocumentId) -> Document | None:
        return self.documents.get(document_id.value)

    async def get_by_content_hash(self, content_hash: ContentHash) -> Document | None:
        for document in self.documents.values():
            if document.content_hash.value == content_hash.value:
                return document
        return None


class FixedClock(IClock):
    """Clock that always returns a preset instant (deterministic tests)."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class SequentialIdGenerator(IIdGenerator):
    """Deterministic id generator producing UUIDs from an incrementing counter."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return str(uuid.UUID(int=self._counter))
