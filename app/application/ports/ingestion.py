"""Framework-free ingestion boundaries; large stage outputs stay outside job checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.documents.ingestion import IngestionOptions, IngestionStage, TextBlock
from app.domain.jobs.entities import Job


@dataclass(frozen=True, slots=True)
class TokenizedText:
    offsets: list[tuple[int, int]]
    # Maximum content tokens the embedding model accepts, excluding special tokens.
    embedding_limit: int


class ITextTokenizer(Protocol):
    async def tokenize(self, text: str) -> TokenizedText: ...


class IDocumentExtractor(Protocol):
    async def extract(self, source: bytes, media_type: str) -> list[TextBlock]: ...


@dataclass(frozen=True, slots=True)
class IngestionUpload:
    document_id: UUID
    user_id: UUID
    filename: str
    content_hash: str
    media_type: str
    version: int
    source: bytes
    options: IngestionOptions


@dataclass(frozen=True, slots=True)
class IngestionDocument:
    id: UUID
    user_id: UUID
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
    options: IngestionOptions


class IIngestionStore(Protocol):
    async def accept(self, upload: IngestionUpload, job: Job) -> IngestionDocument:
        """Atomically persist source + document + PENDING job, or reuse its existing job.

        Concurrent duplicates share one active/completed job. A failed/cancelled job
        gets a new attempt referencing the previously committed stage artifacts.
        """
        ...

    async def get(self, document_id: UUID) -> IngestionDocument: ...

    async def source(self, document_id: UUID) -> bytes: ...

    async def artifact(self, document_id: UUID, key: str) -> dict[str, Any] | None: ...

    async def start_stage(
        self, document_id: UUID, stage: IngestionStage, now: datetime
    ) -> None: ...

    async def save_artifact(
        self,
        document_id: UUID,
        key: str,
        data: dict[str, Any],
        stage: IngestionStage,
        now: datetime,
        *,
        items: int = 0,
        complete: bool = False,
    ) -> None:
        """Commit artifact and stage progress together before the job checkpoint."""
        ...

    async def fail(
        self, document_id: UUID, stage: IngestionStage, message: str, now: datetime
    ) -> None: ...

    async def complete(self, document_id: UUID, chunk_count: int, now: datetime) -> None: ...
