"""Real #8/#12/#18/#20 wiring with deliberately hostile deterministic model ports.

Only model computation and the dataset size differ from the normal worker.
No mock authorization, approval, retrieval, ingestion, traces or job persistence.
This is CI test wiring, never the model profile used for committed measurements.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.application.evaluation.data import canonical_hash
from app.application.evaluation.service import EvaluationService
from app.application.ports.reranking import RerankScore
from app.core.config import Settings
from app.core.container import JobRuntime, build_job_runtime
from app.core.evaluation import build_evaluation_components
from app.infrastructure.evaluation.catalog import FileEvaluationCatalog
from app.infrastructure.system.clock import SystemClock
from tests.support.evaluation_fakes import FakeVersions
from tests.support.ingestion_fakes import FakeTokenizer
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM, StubReranker

ROOT = Path(__file__).resolve().parents[2]


class AttackCatalog:
    def load(self):
        full = FileEvaluationCatalog(
            str(ROOT / "data/evaluation/golden.v2.json"), str(ROOT / "data/corpus/manifest.json")
        ).load()
        cases = tuple(case for case in full.cases if case.containment)
        digest = canonical_hash({"full": full.sha256, "ci_subset": [case.id for case in cases]})
        return replace(
            full,
            version="ticket13-ci-model-stubs-" + digest[:16],
            sha256=digest,
            cases=cases,
            sources=tuple(source for source in full.sources if not source.trusted),
        )


class OfflineEmbeddings(StubEmbeddings, FakeTokenizer):
    def __init__(self, model_name: str) -> None:
        FakeTokenizer.__init__(self)
        assert model_name == "all-MiniLM-L6-v2"

    def close(self) -> None:
        pass


class AttackReranker(StubReranker):
    async def rerank(self, query, candidates):
        # A worst-case cross-encoder promotes relevant poisoned text; the answer
        # guard must hold even when the attack ranks highly. PostgreSQL candidate
        # search and application RRF remain real and unmodified.
        card = next((name for name in ("amber", "cedar", "violet") if name in query.lower()), "")
        return [
            RerankScore(
                candidate.chunk_id,
                0.95 if not card or card in candidate.snippet.lower() else 0.1,
            )
            for candidate in candidates
        ]


def _offline_components(settings, database, embeddings, retrieval, jobs, clock, llm_factory):
    with patch("app.core.evaluation.LocalCrossEncoderReranker", return_value=AttackReranker()):
        return build_evaluation_components(
            settings, database, embeddings, retrieval, jobs, clock, lambda: StubLLM()
        )


def build_test_runtime(settings: Settings) -> JobRuntime:
    with (
        patch("app.core.container.LocalEmbeddingAdapter", OfflineEmbeddings),
        patch("app.core.container.build_evaluation_components", _offline_components),
    ):
        runtime = build_job_runtime(settings)
    assert runtime.evaluation_components is not None
    components = runtime.evaluation_components
    catalog, versions = AttackCatalog(), FakeVersions()
    versions.value.update(
        embedding={"model": "all-MiniLM-L6-v2", "dim": 384, "version": "1"},
        measurement_profile={"models": "ci-only-hostile-model-stubs"},
    )
    components.handler._catalog = catalog
    components.handler._versions = versions
    runtime.evaluation = EvaluationService(
        runtime.service, catalog, components.artifacts, components.authorization, SystemClock()
    )
    return runtime


if __name__ == "__main__":
    runtime = build_test_runtime(Settings())
    runtime.celery_app.worker_main(["worker", *sys.argv[1:]])
