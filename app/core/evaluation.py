"""Compose evaluation with shared #6/#7/#9/#20 adapters; models load only in a worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.application.auth.authorization import AuthorizationService
from app.application.evaluation.handler import EvaluationJobHandler
from app.application.evaluation.probe import EvaluationAuditCapture, EvaluationProbe
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.jobs import IJobStore
from app.application.ports.llm import ILLMProvider
from app.application.ports.retrieval import IRetrievalStore
from app.application.ports.system import IClock
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.core.config import Settings
from app.core.knowledge import build_hybrid_retrieval
from app.infrastructure.audit.logging_sink import LoggingAuditSink
from app.infrastructure.audit.retrieval_sink import PostgresRetrievalAuditSink
from app.infrastructure.evaluation.catalog import FileEvaluationCatalog
from app.infrastructure.evaluation.store import (
    PostgresEvaluationArtifacts,
    PostgresEvaluationEvidence,
)
from app.infrastructure.evaluation.versions import RuntimeEvaluationVersions
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider
from app.infrastructure.reranking.local_adapter import (
    MODEL_NAME,
    MODEL_REVISION,
    LocalCrossEncoderReranker,
)
from app.infrastructure.system.identifiers import UuidGenerator


@dataclass
class EvaluationComponents:
    handler: EvaluationJobHandler
    catalog: FileEvaluationCatalog
    artifacts: PostgresEvaluationArtifacts
    authorization: AuthorizationService
    reranker: LocalCrossEncoderReranker


def build_evaluation_components(
    settings: Settings,
    database: Database,
    embeddings: IEmbeddingProvider,
    retrieval: IRetrievalStore,
    jobs: IJobStore,
    clock: IClock,
    llm_factory: Callable[[], ILLMProvider],
) -> EvaluationComponents:
    catalog = FileEvaluationCatalog(
        settings.evaluation.dataset_path, settings.evaluation.corpus_manifest
    )
    artifacts = PostgresEvaluationArtifacts(database.session_factory)
    evidence = PostgresEvaluationEvidence(database.session_factory)
    users = SqlUserRepository(database.session_factory)
    authorization = AuthorizationService(SqlOwnershipQuery(database.session_factory))
    reranker = LocalCrossEncoderReranker(
        device=settings.reranker.device,
        batch_size=settings.reranker.batch_size,
        max_length=settings.reranker.max_length,
        cache_directory=settings.reranker.cache_directory,
    )
    capture = EvaluationAuditCapture(
        PostgresRetrievalAuditSink(database.session_factory, LoggingAuditSink())
    )
    observer = RetrievalObserver(capture, clock)

    def get_probe() -> EvaluationProbe:
        # Celery's runner opens a fresh event loop per task. Model weights are
        # reusable, HTTP connection pools are not: scope the LLM to this run.
        ids = UuidGenerator()
        llm = llm_factory()
        ask = AskUseCase(
            build_hybrid_retrieval(
                settings, retrieval, embeddings, reranker, authorization, observer, ids
            ),
            llm,
            YamlPromptProvider(settings.prompts.directory, strict=settings.prompts.strict),
            observer,
            ids,
            max_tokens=settings.llm.max_tokens,
            timeout_seconds=settings.llm.timeout_seconds,
        )
        return EvaluationProbe(ask, capture, close=getattr(llm, "aclose", None))

    versions = RuntimeEvaluationVersions(
        {
            "llm": {
                "provider": settings.llm.provider.strip().lower(),
                "model": settings.llm.model,
                "fallback": (settings.llm.fallback or "").strip().lower(),
                "temperature": 0.0,
                "max_tokens": settings.llm.max_tokens,
                "timeout_seconds": settings.llm.timeout_seconds,
            },
            "embedding": {
                "provider": settings.embedding.provider,
                "model": settings.embedding.model,
                "dim": settings.embedding.dimensions,
                "version": settings.embedding.version,
            },
            "reranker": {
                "model": MODEL_NAME,
                "revision": MODEL_REVISION,
                **settings.reranker.model_dump(exclude={"cache_directory"}),
            },
            "retrieval": settings.retrieval.model_dump(),
            "prompt": {"id": "grounded_answer", "version": 2},
            "execution": {"case_order": "dataset", "concurrency": 1},
        },
        root=Path(__file__).resolve().parents[2],
        ollama_url=settings.llm.ollama_base_url,
        prompt_directory=settings.prompts.directory,
    )
    handler = EvaluationJobHandler(
        catalog, evidence, artifacts, versions, users, authorization, jobs, get_probe, clock
    )
    return EvaluationComponents(handler, catalog, artifacts, authorization, reranker)
