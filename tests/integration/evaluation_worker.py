"""Production T7/evaluation/retrieval with only model and small-dataset test ports.

The committed baseline uses the full golden set and real MiniLM/BGE/Ollama.
This worker makes crash/control tests fast and deterministic in regular CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.application.auth.context import Principal
from app.application.evaluation.data import SourcePin
from app.application.evaluation.probe import EvaluationAuditCapture, EvaluationProbe
from app.application.evaluation.service import EvaluationService
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.core.config import Settings
from app.core.container import JobRuntime, build_job_runtime
from app.core.knowledge import build_hybrid_retrieval
from app.infrastructure.audit.logging_sink import LoggingAuditSink
from app.infrastructure.audit.retrieval_sink import PostgresRetrievalAuditSink
from app.infrastructure.evaluation.store import PostgresEvaluationArtifacts
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore
from app.infrastructure.system.clock import SystemClock
from app.infrastructure.system.identifiers import UuidGenerator
from tests.support.evaluation_fakes import TEXT, FakeCatalog, FakeVersions
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM, StubPrompts, StubReranker

SOURCE_HASH = hashlib.sha256(TEXT.encode()).hexdigest()


class QueueTestCatalog(FakeCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.dataset = replace(
            self.dataset,
            sources=(SourcePin("clean", SOURCE_HASH, "text/markdown", 1, True),),
            cases=(
                replace(self.dataset.cases[0], query="source versions"),
                replace(self.dataset.cases[1], query="fictional clinic"),
                replace(
                    self.dataset.cases[2],
                    query="What dosage of an unknown medicine is required?",
                    safety_tags=("dosage",),
                ),
            ),
        )


class InterruptibleArtifacts(PostgresEvaluationArtifacts):
    async def write(self, job_id: UUID, key: str, payload: dict[str, Any], now: datetime) -> None:
        await super().write(job_id, key, payload, now)
        if key == os.environ.get("T12_TEST_PAUSE_AFTER_ARTIFACT"):
            await asyncio.Event().wait()


class CountingProbe(EvaluationProbe):
    """Count actual case executions durably, independently of best-effort tracing."""

    def __init__(self, ask: AskUseCase, capture: EvaluationAuditCapture, engine: Engine) -> None:
        super().__init__(ask, capture)
        self._engine = engine

    async def execute(self, query: str, principal: Principal) -> dict[str, Any]:
        def record_call() -> None:
            with self._engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO evaluation_test_calls(query, executions) VALUES (:query, 1) "
                        "ON CONFLICT(query) DO UPDATE "
                        "SET executions=evaluation_test_calls.executions+1"
                    ),
                    {"query": query},
                )

        await asyncio.to_thread(record_call)
        return await super().execute(query, principal)


def build_test_runtime(settings: Settings) -> JobRuntime:
    runtime = build_job_runtime(settings)
    assert runtime.database is not None and runtime.evaluation_components is not None
    components = runtime.evaluation_components
    clock = SystemClock()
    catalog = QueueTestCatalog()
    versions = FakeVersions()
    versions.value["embedding"]["model"] = "all-MiniLM-L6-v2"
    artifacts = InterruptibleArtifacts(runtime.database.session_factory)
    capture = EvaluationAuditCapture(
        PostgresRetrievalAuditSink(runtime.database.session_factory, LoggingAuditSink())
    )
    observer, ids = RetrievalObserver(capture, clock), UuidGenerator()
    store = PostgresRetrievalStore(
        runtime.database.session_factory,
        embedding_model="all-MiniLM-L6-v2",
        embedding_dim=384,
        embedding_version="1",
    )
    ask = AskUseCase(
        build_hybrid_retrieval(
            settings,
            store,
            StubEmbeddings(),
            StubReranker(),
            components.authorization,
            observer,
            ids,
        ),
        StubLLM(),
        StubPrompts(),
        observer,
        ids,
    )
    components.handler._catalog = catalog
    components.handler._versions = versions
    components.handler._artifacts = artifacts
    components.handler._probe = lambda: CountingProbe(ask, capture, runtime.engine)
    runtime.evaluation = EvaluationService(
        runtime.service, catalog, artifacts, components.authorization, clock
    )
    return runtime


if __name__ == "__main__":
    runtime = build_test_runtime(Settings())
    runtime.celery_app.worker_main(["worker", *sys.argv[1:]])
