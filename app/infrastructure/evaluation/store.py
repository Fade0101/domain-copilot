"""Reuse #6 tables/sessions, #8 source identities and #9 indexed evidence.

Large per-case reports live in existing job_events. T7 checkpoints contain only
small references. A deterministic event UUID also closes the event/checkpoint
crash window without repeating an already persisted model invocation.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import JobStoreError
from app.application.evaluation.data import (
    EVALUATION_OPERATION,
    EvaluationSetupError,
    EvidenceSnapshot,
    GoldenSet,
    IndexedEvidence,
    canonical_hash,
)
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvalidStateTransitionError
from app.infrastructure.persistence.models import (
    ChunkEmbeddingModel,
    ChunkModel,
    DocumentModel,
    DocumentSourceModel,
    JobEventModel,
    JobModel,
)


class PostgresEvaluationArtifacts:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _id(job_id: UUID, key: str) -> UUID:
        return uuid5(job_id, "evaluation-v1/" + key)

    async def read(self, job_id: UUID, key: str) -> dict[str, Any] | None:
        try:
            async with self._sessions() as session:
                return await session.scalar(
                    sa.select(JobEventModel.payload).where(
                        JobEventModel.id == self._id(job_id, key)
                    )
                )
        except SQLAlchemyError:
            raise JobStoreError("Evaluation artifacts are unavailable.") from None

    async def write(self, job_id: UUID, key: str, payload: dict[str, Any], now: datetime) -> None:
        try:
            async with self._sessions() as session, session.begin():
                # Serialize sequence allocation with other writers to this job's log.
                operation = await session.scalar(
                    sa.select(JobModel.operation_type)
                    .where(JobModel.id == job_id)
                    .with_for_update()
                )
                if operation != EVALUATION_OPERATION:
                    raise EvaluationSetupError("NOT_AN_EVALUATION_JOB")
                sequence = await session.scalar(
                    sa.select(
                        sa.func.coalesce(sa.func.max(JobEventModel.sequence_number), 0)
                    ).where(JobEventModel.job_id == job_id)
                )
                await session.execute(
                    insert(JobEventModel)
                    .values(
                        id=self._id(job_id, key),
                        job_id=job_id,
                        sequence_number=(sequence or 0) + 1,
                        event_type="evaluation." + ("case" if key.startswith("case/") else key),
                        payload=payload,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[JobEventModel.id])
                )
        except SQLAlchemyError:
            raise JobStoreError("Evaluation artifacts are unavailable.") from None

    async def request_cancel(self, job_id: UUID, now: datetime) -> None:
        try:
            async with self._sessions() as session, session.begin():
                result = await session.execute(
                    sa.update(JobModel)
                    .where(
                        JobModel.id == job_id,
                        JobModel.operation_type == EVALUATION_OPERATION,
                        JobModel.state.in_(
                            [JobState.PENDING.value, JobState.QUEUED.value, JobState.STARTED.value]
                        ),
                    )
                    .values(cancellation_requested=True, updated_at=now)
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    raise InvalidStateTransitionError("Only an active evaluation can be cancelled.")
        except SQLAlchemyError:
            raise JobStoreError("Evaluation artifacts are unavailable.") from None


class PostgresEvaluationEvidence:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def snapshot(self, dataset: GoldenSet) -> EvidenceSnapshot:
        try:
            return await self._snapshot(dataset)
        except SQLAlchemyError:
            raise JobStoreError("Evaluation evidence is unavailable.") from None

    async def _snapshot(self, dataset: GoldenSet) -> EvidenceSnapshot:
        pins = {(pin.sha256, pin.media_type, pin.version): pin for pin in dataset.sources}
        async with self._sessions() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            documents = list(
                (await session.scalars(sa.select(DocumentModel).order_by(DocumentModel.id))).all()
            )
            if len(documents) != len(pins):
                raise EvaluationSetupError("CORPUS_INVENTORY_MISMATCH")
            mapped: dict[str, str] = {}
            configs = {}
            rows: list[dict[str, Any]] = []
            for document in documents:
                pin = pins.get(
                    (document.content_hash or "", document.media_type or "", document.version)
                )
                if pin is None or document.status != "COMPLETED" or pin.id in mapped.values():
                    raise EvaluationSetupError("CORPUS_INVENTORY_MISMATCH")
                mapped[str(document.id)] = pin.id
                configs[pin.id] = document.ingestion_config
                rows.append(
                    {
                        "id": str(document.id),
                        "name": document.filename,
                        "source": pin.id,
                        "hash": document.content_hash,
                        "config": document.ingestion_config,
                        "chunk_count": document.chunk_count,
                    }
                )
            if set(mapped.values()) != {pin.id for pin in dataset.sources}:
                raise EvaluationSetupError("CORPUS_INVENTORY_MISMATCH")
            source_pins = {pin.id: pin for pin in dataset.sources}
            source_rows = (
                await session.execute(
                    sa.select(DocumentSourceModel.document_id, DocumentSourceModel.source)
                )
            ).all()
            if len(source_rows) != len(documents):
                raise EvaluationSetupError("CORPUS_SOURCE_BYTES_MISSING")
            for source_document_id, source in source_rows:
                source_id = mapped.get(str(source_document_id))
                if (
                    source_id is None
                    or hashlib.sha256(source).hexdigest() != source_pins[source_id].sha256
                ):
                    raise EvaluationSetupError("CORPUS_SOURCE_CHECKSUM_MISMATCH")
            chunks: dict[str, IndexedEvidence] = {}
            filenames = {str(d.id): d.filename for d in documents}
            document_versions = {str(d.id): d.version for d in documents}
            counts: dict[str, int] = dict.fromkeys(mapped, 0)
            for chunk in (
                await session.scalars(sa.select(ChunkModel).order_by(ChunkModel.id))
            ).all():
                document_id = str(chunk.document_id)
                if document_id not in mapped:
                    raise EvaluationSetupError("UNPINNED_CHUNK")
                if chunk.document_version != document_versions[document_id]:
                    raise EvaluationSetupError("CHUNK_VERSION_MISMATCH")
                chunk_id = str(chunk.id)
                source_id = mapped[document_id]
                chunks[chunk_id] = IndexedEvidence(
                    chunk_id,
                    document_id,
                    source_id,
                    filenames[document_id],
                    chunk.section,
                    chunk.page,
                    chunk.text,
                    source_pins[source_id].trusted,
                )
                counts[document_id] += 1
                rows.append(
                    {
                        "chunk_id": chunk_id,
                        "document_id": document_id,
                        "text": chunk.text,
                        "section": chunk.section,
                        "page": chunk.page,
                        "version": chunk.document_version,
                        "tokens": chunk.token_count,
                    }
                )
            if any(not counts[str(d.id)] or counts[str(d.id)] != d.chunk_count for d in documents):
                raise EvaluationSetupError("INCOMPLETE_CORPUS_CHUNKS")
            embedded: set[str] = set()
            embeddings = await session.execute(
                sa.select(
                    ChunkEmbeddingModel.chunk_id,
                    ChunkEmbeddingModel.embedding_model,
                    ChunkEmbeddingModel.embedding_version,
                    ChunkEmbeddingModel.embedding_dim,
                    sa.func.md5(sa.cast(ChunkEmbeddingModel.embedding, sa.Text)).label(
                        "vector_hash"
                    ),
                ).order_by(
                    ChunkEmbeddingModel.chunk_id,
                    ChunkEmbeddingModel.embedding_model,
                    ChunkEmbeddingModel.embedding_version,
                )
            )
            for embedding in embeddings.mappings():
                item = dict(embedding)
                item["chunk_id"] = str(item["chunk_id"])
                rows.append(item)
                if item["chunk_id"] not in chunks:
                    raise EvaluationSetupError("UNPINNED_EMBEDDING")
                config = configs[chunks[item["chunk_id"]].source_id]
                if all(
                    config.get(key) == item[key]
                    for key in ("embedding_model", "embedding_dim", "embedding_version")
                ):
                    embedded.add(item["chunk_id"])
            if embedded != set(chunks):
                raise EvaluationSetupError("INCOMPLETE_CORPUS_EMBEDDINGS")
            return EvidenceSnapshot(
                canonical_hash(rows),
                chunks,
                {source: doc for doc, source in mapped.items()},
                configs,
            )
