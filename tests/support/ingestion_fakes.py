"""SDK-free ingestion doubles shared by application and worker integration tests."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.errors import ResourceNotFoundError
from app.application.ports.embeddings import EmbeddingResult
from app.application.ports.ingestion import IngestionDocument, IngestionUpload, TokenizedText
from app.domain.documents.ingestion import IngestionStage, TextBlock
from app.domain.jobs.entities import Job, JobState
from tests.support.job_fakes import FakeJobStore


class FakeTokenizer:
    def __init__(self, embedding_limit: int = 254) -> None:
        self.embedding_limit = embedding_limit
        self.failure: Exception | None = None

    async def tokenize(self, text: str) -> TokenizedText:
        if self.failure:
            raise self.failure
        return TokenizedText(
            [match.span() for match in re.finditer(r"\w+|[^\w\s]", text)],
            self.embedding_limit,
        )


class RecordingEmbeddings:
    def __init__(self, *, model: str = "test-model", dimensions: int = 4) -> None:
        self.model = model
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.failure: Exception | None = None

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.failure:
            raise self.failure
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            vector[0] = 1.0
            vector[1] = float(text.lower().count("alpha"))
            vector[2] = float(text.lower().count("beta"))
            vector[3] = float(text.lower().count("gamma"))
            vectors.append(vector)
        return EmbeddingResult(vectors, self.model, self.dimensions)


class FakeExtractor:
    def __init__(self, blocks: list[TextBlock] | None = None) -> None:
        self.blocks = blocks
        self.calls = 0
        self.failure: Exception | None = None

    async def extract(self, source: bytes, media_type: str) -> list[TextBlock]:
        self.calls += 1
        if self.failure:
            raise self.failure
        return (
            self.blocks
            if self.blocks is not None
            else [TextBlock(source.decode(), 1, ("Synthetic",))]
        )


class FakeIngestionStore:
    def __init__(self, jobs: FakeJobStore) -> None:
        self.jobs = jobs
        self.documents: dict[UUID, IngestionDocument] = {}
        self.sources: dict[UUID, bytes] = {}
        self.artifacts: dict[tuple[UUID, str], dict[str, Any]] = {}

    async def accept(self, upload: IngestionUpload, job: Job) -> IngestionDocument:
        existing = self.documents.get(upload.document_id)
        if existing and self.jobs.jobs[existing.job_id].state not in {
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return deepcopy(existing)
        await self.jobs.add(job)
        if existing:
            stages = deepcopy(existing.stages)
            for progress in stages.values():
                if progress["status"] == "FAILED":
                    progress.update(status="PENDING", error=None)
            document = replace(
                existing,
                job_id=job.id,
                status="PENDING",
                stages=stages,
                error_stage=None,
                error_message=None,
            )
        else:
            document = IngestionDocument(
                id=upload.document_id,
                user_id=upload.user_id,
                filename=upload.filename,
                content_hash=upload.content_hash,
                media_type=upload.media_type,
                version=upload.version,
                status="PENDING",
                job_id=job.id,
                stages={stage.value: {"status": "PENDING"} for stage in IngestionStage},
                error_stage=None,
                error_message=None,
                created_at=job.created_at,
                ingested_at=None,
                chunk_count=0,
                options=upload.options,
            )
        self.documents[document.id] = document
        self.sources.setdefault(document.id, upload.source)
        return deepcopy(document)

    async def get(self, document_id: UUID) -> IngestionDocument:
        if document_id not in self.documents:
            raise ResourceNotFoundError("Document not found.")
        return deepcopy(self.documents[document_id])

    async def source(self, document_id: UUID) -> bytes:
        return self.sources[document_id]

    async def artifact(self, document_id: UUID, key: str) -> dict[str, Any] | None:
        return deepcopy(self.artifacts.get((document_id, key)))

    async def start_stage(self, document_id: UUID, stage: IngestionStage, now: datetime) -> None:
        document = await self.get(document_id)
        document.stages[stage.value].update(
            status="PROCESSING", error=None, started_at=now.isoformat()
        )
        self.documents[document_id] = replace(
            document, status="PROCESSING", error_stage=None, error_message=None
        )

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
        self.artifacts.setdefault((document_id, key), deepcopy(data))
        document = await self.get(document_id)
        document.stages[stage.value]["items"] = items
        if complete:
            document.stages[stage.value].update(
                status="COMPLETED", error=None, completed_at=now.isoformat()
            )
        self.documents[document_id] = document

    async def fail(
        self, document_id: UUID, stage: IngestionStage, message: str, now: datetime
    ) -> None:
        document = await self.get(document_id)
        document.stages[stage.value].update(
            status="FAILED", error=message, failed_at=now.isoformat()
        )
        self.documents[document_id] = replace(
            document, status="FAILED", error_stage=stage.value, error_message=message
        )

    async def complete(self, document_id: UUID, chunk_count: int, now: datetime) -> None:
        self.documents[document_id] = replace(
            await self.get(document_id),
            status="COMPLETED",
            ingested_at=now,
            chunk_count=chunk_count,
            error_stage=None,
            error_message=None,
        )
