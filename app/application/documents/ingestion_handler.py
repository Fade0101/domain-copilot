"""Durable extract -> clean -> chunk -> embed -> index handler on the T7 runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.documents.chunking import StructureAwareChunker, clean_blocks
from app.application.documents.embedding import IngestionEmbedder
from app.application.documents.ingestion_service import INGEST_OPERATION
from app.application.errors import IngestionError, JobCancelled, JobPaused, JobStoreError
from app.application.ports.ingestion import IDocumentExtractor, IIngestionStore, IngestionDocument
from app.application.ports.jobs import IJobContext
from app.application.ports.retrieval import ChunkRecord, IRetrievalStore
from app.application.ports.system import IClock
from app.domain.documents.ingestion import IngestionOptions, IngestionStage, TextBlock
from app.domain.shared.errors import InvariantViolationError


def _blocks(data: dict[str, Any]) -> list[TextBlock]:
    return [
        TextBlock(item["text"], item["page"], tuple(item["headings"])) for item in data["blocks"]
    ]


def _block_data(blocks: list[TextBlock]) -> dict[str, Any]:
    return {
        "blocks": [
            {"text": block.text, "page": block.page, "headings": list(block.headings)}
            for block in blocks
        ]
    }


class DocumentIngestionHandler:
    operation_type = INGEST_OPERATION

    def __init__(
        self,
        store: IIngestionStore,
        extractor: IDocumentExtractor,
        chunker: StructureAwareChunker,
        embedder: IngestionEmbedder,
        retrieval: IRetrievalStore,
        clock: IClock,
        options: IngestionOptions,
        *,
        stage_timeout_seconds: float = 600,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._chunker = chunker
        self._embedder = embedder
        self._retrieval = retrieval
        self._clock = clock
        self._options = options
        self._timeout = stage_timeout_seconds

    def validate(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"document_id"} or not isinstance(payload["document_id"], str):
            raise InvariantViolationError("Ingestion jobs require exactly one document_id.")
        try:
            UUID(payload["document_id"])
        except ValueError:
            raise InvariantViolationError("document_id must be a UUID.") from None

    async def _step(
        self,
        context: IJobContext,
        document: IngestionDocument,
        stage: IngestionStage,
        key: str,
        action: Callable[[], Awaitable[dict[str, Any]]],
        *,
        items: int = 0,
        complete: bool = True,
    ) -> dict[str, Any]:
        async def persist() -> dict[str, Any]:
            existing = await self._store.artifact(document.id, key)
            data = existing
            if data is None:
                await self._store.start_stage(document.id, stage, self._clock.now())
                data = await asyncio.wait_for(action(), timeout=self._timeout)
            if existing is None or (
                complete and document.stages[stage.value]["status"] != "COMPLETED"
            ):
                # A terminal attempt can fail/cancel after its final artifact.
                # Restore progress when a new attempt reuses that artifact.
                count = items or len(data.get("blocks", data.get("chunks", [])))
                await self._store.save_artifact(
                    document.id,
                    key,
                    data,
                    stage,
                    self._clock.now(),
                    items=count,
                    complete=complete,
                )
            # A crash between the artifact commit and this job checkpoint finds
            # the artifact on redelivery and skips the completed computation.
            return {"document_id": str(document.id), "artifact": key}

        await context.step("ingest:" + key, persist)
        result = await self._store.artifact(document.id, key)
        if result is None:
            raise JobStoreError("A committed ingestion artifact is unavailable.")
        return result

    async def run(self, context: IJobContext) -> dict[str, Any]:
        self.validate(context.payload)
        document = await self._store.get(UUID(context.payload["document_id"]))
        # Generic admin job submission must not overwrite another upload or run
        # an extra job against the same document outside atomic acceptance.
        if document.user_id != context.user_id or document.job_id != context.job_id:
            raise InvariantViolationError("This job does not own the document ingestion attempt.")
        options = document.options
        stage = next(
            (
                item
                for item in IngestionStage
                if document.stages[item.value]["status"] != "COMPLETED"
            ),
            IngestionStage.INDEX,
        )
        try:
            if (options.embedding_model, options.embedding_dim, options.embedding_version) != (
                self._options.embedding_model,
                self._options.embedding_dim,
                self._options.embedding_version,
            ):
                raise IngestionError(
                    "Restore the upload's embedding model and version before resuming."
                )

            stage = IngestionStage.EXTRACT

            async def extract() -> dict[str, Any]:
                return _block_data(
                    await self._extractor.extract(
                        await self._store.source(document.id),
                        document.media_type,
                    )
                )

            extracted = await self._step(context, document, stage, "extract", extract)
            stage = IngestionStage.CLEAN

            async def clean() -> dict[str, Any]:
                return _block_data(clean_blocks(_blocks(extracted)))

            cleaned = await self._step(context, document, stage, "clean", clean)
            stage = IngestionStage.CHUNK

            async def chunk() -> dict[str, Any]:
                chunks = await self._chunker.chunk(document.id, _blocks(cleaned), options)
                return {
                    "ingested_at": self._clock.now().isoformat(),
                    "chunks": [
                        {
                            "id": str(item.id),
                            "text": item.text,
                            "section": item.section,
                            "page": item.page,
                            "token_count": item.token_count,
                        }
                        for item in chunks
                    ],
                }

            chunked = await self._step(context, document, stage, "chunk", chunk)
            chunks = chunked["chunks"]
            count = len(chunks)
            size = options.embedding_batch_size
            stage = IngestionStage.EMBED
            for start in range(0, count, size):
                batch = chunks[start : start + size]

                async def embed(batch: list[dict[str, Any]] = batch) -> dict[str, Any]:
                    return {
                        "vectors": await self._embedder.embed(
                            [item["text"] for item in batch], options
                        )
                    }

                await self._step(
                    context,
                    document,
                    stage,
                    f"embed:{start}",
                    embed,
                    items=start + len(batch),
                    complete=False,
                )

            async def manifest() -> dict[str, Any]:
                return {"chunk_count": count, "batch_size": size}

            await self._step(context, document, stage, "embed", manifest, items=count)
            stage = IngestionStage.INDEX
            for start in range(0, count, size):
                batch = chunks[start : start + size]

                async def index(
                    start: int = start, batch: list[dict[str, Any]] = batch
                ) -> dict[str, Any]:
                    embedded = await self._store.artifact(document.id, f"embed:{start}")
                    if embedded is None:
                        raise JobStoreError("A committed embedding batch is unavailable.")
                    await self._retrieval.upsert_chunks(
                        [
                            ChunkRecord(
                                chunk_id=UUID(item["id"]),
                                document_id=document.id,
                                text=item["text"],
                                section=item["section"],
                                page=item["page"],
                                token_count=item["token_count"],
                                document_version=document.version,
                                ingested_at=datetime.fromisoformat(chunked["ingested_at"]),
                                embedding=vector,
                                embedding_model=options.embedding_model,
                                embedding_dim=options.embedding_dim,
                                embedding_version=options.embedding_version,
                            )
                            for item, vector in zip(batch, embedded["vectors"], strict=True)
                        ]
                    )
                    # If the process dies after the retrieval commit, repeating
                    # this upsert uses the same deterministic chunk/provenance keys.
                    return {"chunk_count": len(batch)}

                await self._step(
                    context,
                    document,
                    stage,
                    f"index:{start}",
                    index,
                    items=start + len(batch),
                    complete=False,
                )
            await self._step(context, document, stage, "index", manifest, items=count)

            async def finish() -> dict[str, Any]:
                await self._store.complete(document.id, count, self._clock.now())
                return {
                    "document_id": str(document.id),
                    "version": document.version,
                    "chunk_count": count,
                    "status": "COMPLETED",
                }

            return await context.step("ingest:complete", finish)
        except (JobStoreError, JobPaused):
            raise
        except JobCancelled:
            await self._store.fail(
                document.id, stage, "Ingestion was cancelled.", self._clock.now()
            )
            raise
        except Exception as exc:
            message = (
                str(exc) if isinstance(exc, IngestionError) else f"The {stage.value} stage failed."
            )
            await self._store.fail(document.id, stage, message, self._clock.now())
            raise IngestionError(message) from None
