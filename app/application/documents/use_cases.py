"""The RegisterDocument use case -- the representative vertical slice.

Exercises three ports (repository, clock, id generator), enforces the BRD
idempotency rule by ``content_hash``, and returns a view plus a ``created`` flag
the presentation layer maps to HTTP 201 vs 200. Depends only on ports and the
domain -- never on a concrete adapter, a framework, or an SDK.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.documents.commands import RegisterDocumentCommand
from app.application.documents.dto import DocumentView
from app.application.ports.repositories import IDocumentRepository
from app.application.ports.system import IClock, IIdGenerator
from app.domain.documents.entities import Document
from app.domain.documents.value_objects import ContentHash, DocumentId, Filename


@dataclass(frozen=True, slots=True)
class RegisterDocumentResult:
    """Outcome of :class:`RegisterDocumentUseCase`.

    ``created`` is ``True`` when a new document was persisted, ``False`` when an
    existing document with the same content hash was returned idempotently.
    """

    document: DocumentView
    created: bool


class RegisterDocumentUseCase:
    """Register a document's metadata prior to asynchronous ingestion."""

    def __init__(
        self,
        repository: IDocumentRepository,
        clock: IClock,
        id_generator: IIdGenerator,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, command: RegisterDocumentCommand) -> RegisterDocumentResult:
        # Validate the content hash first; it is the idempotency key.
        content_hash = ContentHash(command.content_hash)

        existing = await self._repository.get_by_content_hash(content_hash)
        if existing is not None:
            # Idempotent re-registration: return the existing document. Do NOT
            # create a second document and do NOT overwrite its metadata.
            return RegisterDocumentResult(
                document=DocumentView.from_entity(existing),
                created=False,
            )

        document = Document.register(
            document_id=DocumentId(self._id_generator.new_id()),
            filename=Filename(command.filename),
            content_hash=content_hash,
            registered_at=self._clock.now(),
            metadata=command.metadata,
        )
        await self._repository.add(document)
        return RegisterDocumentResult(
            document=DocumentView.from_entity(document),
            created=True,
        )
