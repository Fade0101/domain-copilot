"""Run the production ingestion handler with deterministic offline embedding ports.

Parsing, persistence, retrieval SQL, Celery and checkpointing are real. Only the
model/tokenizer are test doubles; Docker smoke testing exercises the local model.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from app.application.documents.chunking import StructureAwareChunker
from app.application.documents.embedding import IngestionEmbedder
from app.application.documents.ingestion_handler import DocumentIngestionHandler
from app.application.documents.ingestion_service import IngestionService
from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.ports.embeddings import EmbeddingResult
from app.core.config import Settings
from app.core.container import JobRuntime, build_ingestion_options, build_job_runtime
from app.domain.documents.ingestion import IngestionStage
from app.infrastructure.ingestion.extractors import DocumentExtractor
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.job_store import create_job_engine
from app.infrastructure.persistence.sql.ingestion_store import PostgresIngestionStore
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore
from app.infrastructure.system.clock import SystemClock
from tests.support.ingestion_fakes import FakeTokenizer, RecordingEmbeddings


class CountedEmbeddings(RecordingEmbeddings):
    def __init__(self, url: str, model: str, dimensions: int) -> None:
        super().__init__(model=model, dimensions=dimensions)
        self._url = url

    def _record(self, texts: list[str]) -> None:
        engine = create_job_engine(self._url)
        try:
            with engine.begin() as connection:
                for text in texts:
                    connection.execute(
                        sa.text(
                            "INSERT INTO t8_embedding_calls (digest, calls) VALUES (:digest, 1) "
                            "ON CONFLICT (digest) DO UPDATE "
                            "SET calls = t8_embedding_calls.calls + 1"
                        ),
                        {"digest": hashlib.sha256(text.encode()).hexdigest()},
                    )
        finally:
            engine.dispose()

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        await asyncio.to_thread(self._record, texts)
        return await super().generate_embeddings(texts)


class InterruptibleIngestionStore(PostgresIngestionStore):
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
        await super().save_artifact(
            document_id, key, data, stage, now, items=items, complete=complete
        )
        if key == os.environ.get("T8_TEST_PAUSE_AFTER_ARTIFACT"):
            # Killed by the owning test after it observes the PG artifact. The
            # checkpoint for this step has deliberately not committed yet.
            await asyncio.Event().wait()


def build_test_runtime(settings: Settings) -> JobRuntime:
    assert settings.database.url
    database = Database(settings.database.url, pooling=False)
    store = InterruptibleIngestionStore(database.session_factory)
    tokenizer = FakeTokenizer()
    embeddings = CountedEmbeddings(
        settings.database.url, settings.embedding.model, settings.embedding.dimensions
    )
    options = build_ingestion_options(settings)
    retrieval = PostgresRetrievalStore(
        database.session_factory,
        embedding_model=options.embedding_model,
        embedding_dim=options.embedding_dim,
        embedding_version=options.embedding_version,
    )
    handler = DocumentIngestionHandler(
        store,
        DocumentExtractor(max_pages=500, max_characters=2_000_000),
        StructureAwareChunker(tokenizer),
        IngestionEmbedder(embeddings, tokenizer),
        retrieval,
        SystemClock(),
        options,
    )
    runtime = build_job_runtime(
        settings, handlers=[DiagnosticJobHandler(), handler], database=database
    )
    runtime.owns_database = True
    runtime.ingestion = IngestionService(
        store, runtime.service, options, max_upload_bytes=settings.ingestion.max_upload_bytes
    )
    return runtime


if __name__ == "__main__":
    runtime = build_test_runtime(Settings())
    runtime.celery_app.worker_main(["worker", *sys.argv[1:]])
