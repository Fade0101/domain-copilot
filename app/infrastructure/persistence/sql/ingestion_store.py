"""PostgreSQL source/artifact storage and atomic ingestion acceptance (Ticket 8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import JobStoreError, ResourceNotFoundError
from app.application.ports.ingestion import IIngestionStore, IngestionDocument, IngestionUpload
from app.domain.documents.ingestion import IngestionOptions, IngestionStage
from app.domain.jobs.entities import Job
from app.infrastructure.persistence.job_store import job_insert_values
from app.infrastructure.persistence.models import Base

_documents = Base.metadata.tables["documents"]
_sources = Base.metadata.tables["document_sources"]
_artifacts = Base.metadata.tables["ingestion_artifacts"]
_jobs = Base.metadata.tables["jobs"]


def _document(row: RowMapping) -> IngestionDocument:
    return IngestionDocument(
        id=row["id"],
        user_id=row["user_id"],
        filename=row["filename"],
        content_hash=row["content_hash"],
        media_type=row["media_type"],
        version=row["version"],
        status=row["status"],
        job_id=row["ingestion_job_id"],
        stages=row["ingestion_stages"],
        error_stage=row["error_stage"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        ingested_at=row["ingested_at"],
        chunk_count=row["chunk_count"],
        options=IngestionOptions(**row["ingestion_config"]),
    )


class PostgresIngestionStore(IIngestionStore):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except (SQLAlchemyError, OSError):
            # The runner leaves STARTED jobs resumable on lost durable storage.
            # Source bytes and database parameters must never enter an error log.
            raise JobStoreError("Document ingestion storage is unavailable.") from None

    async def accept(self, upload: IngestionUpload, job: Job) -> IngestionDocument:
        async with self._transaction() as session:
            await session.execute(
                insert(_documents)
                .values(
                    id=upload.document_id,
                    user_id=upload.user_id,
                    filename=upload.filename,
                    content="",
                    status="PENDING",
                    metadata={},
                    content_hash=upload.content_hash,
                    media_type=upload.media_type,
                    version=upload.version,
                    created_at=job.created_at,
                    ingestion_config=asdict(upload.options),
                    ingestion_stages={
                        stage.value: {"status": "PENDING"} for stage in IngestionStage
                    },
                )
                .on_conflict_do_nothing(constraint="uq_document_ingestion_source")
            )
            # The row lock serializes duplicate acceptance, including retry after failure.
            row = (
                (
                    await session.execute(
                        sa.select(_documents)
                        .where(_documents.c.id == upload.document_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            previous = row["ingestion_job_id"]
            previous_state = (
                None
                if previous is None
                else await session.scalar(sa.select(_jobs.c.state).where(_jobs.c.id == previous))
            )
            if previous_state in {"PENDING", "QUEUED", "STARTED", "COMPLETED"}:
                return _document(row)
            await session.execute(_jobs.insert().values(**job_insert_values(job)))
            await session.execute(
                insert(_sources)
                .values(document_id=upload.document_id, source=upload.source)
                .on_conflict_do_nothing(index_elements=[_sources.c.document_id])
            )
            stages = deepcopy(row["ingestion_stages"])
            for progress in stages.values():
                if progress["status"] == "FAILED":
                    progress.update(status="PENDING", error=None)
            updated = (
                (
                    await session.execute(
                        _documents.update()
                        .where(_documents.c.id == upload.document_id)
                        .values(
                            ingestion_job_id=job.id,
                            status="PENDING",
                            error_stage=None,
                            error_message=None,
                            ingestion_stages=stages,
                        )
                        .returning(_documents)
                    )
                )
                .mappings()
                .one()
            )
            return _document(updated)

    async def get(self, document_id: UUID) -> IngestionDocument:
        async with self._transaction() as session:
            row = (
                (
                    await session.execute(
                        sa.select(_documents).where(
                            _documents.c.id == document_id,
                            _documents.c.ingestion_job_id.is_not(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ResourceNotFoundError("Ingested document not found.")
            return _document(row)

    async def source(self, document_id: UUID) -> bytes:
        async with self._transaction() as session:
            source = await session.scalar(
                sa.select(_sources.c.source).where(_sources.c.document_id == document_id)
            )
            if source is None:
                raise ResourceNotFoundError("Document source not found.")
            return bytes(source)

    async def artifact(self, document_id: UUID, key: str) -> dict[str, Any] | None:
        async with self._transaction() as session:
            return await session.scalar(
                sa.select(_artifacts.c.payload).where(
                    _artifacts.c.document_id == document_id,
                    _artifacts.c.artifact_key == key,
                )
            )

    async def _stages(self, session: AsyncSession, document_id: UUID) -> dict[str, Any]:
        stages = await session.scalar(
            sa.select(_documents.c.ingestion_stages)
            .where(_documents.c.id == document_id)
            .with_for_update()
        )
        if stages is None:
            raise ResourceNotFoundError("Document not found.")
        return deepcopy(stages)

    async def start_stage(self, document_id: UUID, stage: IngestionStage, now: datetime) -> None:
        async with self._transaction() as session:
            stages = await self._stages(session, document_id)
            progress = stages[stage.value]
            progress.update(status="PROCESSING", error=None)
            progress.setdefault("started_at", now.isoformat())
            await session.execute(
                _documents.update()
                .where(_documents.c.id == document_id)
                .values(
                    status="PROCESSING",
                    ingestion_stages=stages,
                    error_stage=None,
                    error_message=None,
                )
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
        async with self._transaction() as session:
            stages = await self._stages(session, document_id)
            await session.execute(
                insert(_artifacts)
                .values(
                    document_id=document_id,
                    artifact_key=key,
                    payload=data,
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[_artifacts.c.document_id, _artifacts.c.artifact_key]
                )
            )
            progress = stages[stage.value]
            progress["items"] = max(items, progress.get("items", 0))
            if complete:
                progress.update(status="COMPLETED", completed_at=now.isoformat(), error=None)
            values: dict[str, Any] = {"ingestion_stages": stages}
            if key == "clean":
                values["content"] = "\n\n".join(block["text"] for block in data["blocks"])
            await session.execute(
                _documents.update().where(_documents.c.id == document_id).values(**values)
            )

    async def fail(
        self, document_id: UUID, stage: IngestionStage, message: str, now: datetime
    ) -> None:
        async with self._transaction() as session:
            stages = await self._stages(session, document_id)
            stages[stage.value].update(status="FAILED", error=message, failed_at=now.isoformat())
            await session.execute(
                _documents.update()
                .where(_documents.c.id == document_id)
                .values(
                    status="FAILED",
                    error_stage=stage.value,
                    error_message=message,
                    ingestion_stages=stages,
                )
            )

    async def complete(self, document_id: UUID, chunk_count: int, now: datetime) -> None:
        async with self._transaction() as session:
            await session.execute(
                _documents.update()
                .where(_documents.c.id == document_id)
                .values(
                    status="COMPLETED",
                    chunk_count=chunk_count,
                    ingested_at=sa.func.coalesce(_documents.c.ingested_at, now),
                    error_stage=None,
                    error_message=None,
                )
            )
