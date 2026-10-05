"""Evaluation observes exactly one real #10 ask invocation and forwards its traces."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.auth.authorization import AuthorizationService
from app.application.errors import ProviderUnavailableError
from app.application.evaluation.probe import EvaluationAuditCapture, EvaluationProbe
from app.application.qa.grounding import REFUSAL
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from tests.support.fakes import (
    FakeOwnershipQuery,
    FixedClock,
    RecordingAuditSink,
    SequentialIdGenerator,
)
from tests.support.knowledge_fakes import (
    StaticRetrievalStore,
    StubEmbeddings,
    StubLLM,
    StubPrompts,
    StubReranker,
    hit,
    principal,
)


@pytest.mark.parametrize("scenario", ["normal", "empty", "low", "conflict", "error", "invalid"])
async def test_probe_uses_real_fusion_reranking_grounding_and_error_boundaries(
    scenario: str,
) -> None:
    hits = [] if scenario == "empty" else [hit(1)]
    if scenario == "conflict":
        hits = [hit(1, "The review interval is 7 days."), hit(2, "The review interval is 14 days.")]
    store = StaticRetrievalStore(hits, hits)
    audit = RecordingAuditSink()
    capture = EvaluationAuditCapture(audit)
    observer = RetrievalObserver(capture, FixedClock(datetime(2026, 10, 4, tzinfo=UTC)))
    ids = SequentialIdGenerator()
    llm = StubLLM("bad output" if scenario == "invalid" else "auto")
    if scenario == "error":
        llm.failure = ProviderUnavailableError("secret provider detail")
    reranker = StubReranker({item.chunk_id: 0.1 for item in hits} if scenario == "low" else None)
    retrieval = HybridRetrievalUseCase(
        store, StubEmbeddings(), reranker, AuthorizationService(FakeOwnershipQuery()), observer, ids
    )
    result = await EvaluationProbe(
        AskUseCase(retrieval, llm, StubPrompts(), observer, ids), capture
    ).execute("clinic review", principal())
    assert store.calls == [("dense", 20), ("keyword", 20)]
    assert result["retrieval"]["rrf_k"] == 60
    assert {entry.action for entry in audit.entries} == {"retrieval.hybrid", "qa.ask"}
    assert capture.entries.get() is None
    if scenario == "normal":
        assert result["refused"] is False and len(result["citations"]) == 1
    elif scenario == "error":
        assert result["error_phase"] == "generation"
        assert "secret provider detail" not in str(result)
        assert result["refused"] is None
    else:
        assert result["answer"] == REFUSAL and result["refused"] is True


async def test_per_run_provider_cleanup_is_explicit() -> None:
    calls = []

    async def close() -> None:
        calls.append("closed")

    from tests.support.knowledge_fakes import harness

    knowledge = harness()
    probe = EvaluationProbe(knowledge.ask, EvaluationAuditCapture(knowledge.audit), close)
    await probe.aclose()
    assert calls == ["closed"]
