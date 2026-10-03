"""RRF math, independent rankings, confidence filtering and observable failures."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from app.application.errors import KnowledgeUnavailableError, RetrievalStoreError
from app.application.ports.reranking import RerankScore
from app.application.retrieval.fusion import reciprocal_rank_fusion
from app.application.retrieval.use_cases import RetrievalOptions
from app.domain.shared.errors import InvariantViolationError
from tests.support.knowledge_fakes import harness, hit, principal


def test_rrf_defaults_to_sixty_with_one_based_ranks() -> None:
    first, shared, sparse = hit(1), hit(2), hit(3)
    fused = reciprocal_rank_fusion([first, shared], [shared, sparse])
    assert [item.hit.chunk_id for item in fused] == [
        shared.chunk_id,
        first.chunk_id,
        sparse.chunk_id,
    ]
    assert fused[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].rrf_score == pytest.approx(1 / 61)


def test_rrf_k_is_configurable_without_changing_fusion_method() -> None:
    assert reciprocal_rank_fusion([hit(1)], [hit(1)], k=10)[0].rrf_score == pytest.approx(2 / 11)


@pytest.mark.parametrize("k", [0, -1, True, 0.5])
def test_rrf_rejects_invalid_k(k) -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], [], k=k)


def test_rrf_ignores_score_scale_and_ties_break_by_uuid() -> None:
    dense, keyword = hit(2, score=0.001), hit(1, score=900)
    first = reciprocal_rank_fusion([dense], [keyword])
    second = reciprocal_rank_fusion([replace(keyword, score=-5)], [replace(dense, score=40)])
    assert [(item.hit.chunk_id, item.rrf_score) for item in first] == [
        (item.hit.chunk_id, item.rrf_score) for item in second
    ]
    assert first[0].hit.chunk_id == UUID(int=1)


def test_rrf_does_not_double_count_duplicates_within_a_source() -> None:
    first, second = hit(1), hit(2)
    fused = reciprocal_rank_fusion([first, first, second], [first])
    assert len(fused) == 2
    assert fused[0].rrf_score == pytest.approx(2 / 61)
    assert fused[1].dense_rank == 2


def test_inconsistent_metadata_for_one_chunk_is_rejected() -> None:
    with pytest.raises(RetrievalStoreError, match="inconsistent citation"):
        reciprocal_rank_fusion([hit(1)], [replace(hit(1), snippet="different version")])


async def test_both_searches_feed_fusion_then_reranker_changes_order() -> None:
    first, second, third = hit(1), hit(2), hit(3)
    system = harness([first, second], [second, third], scores={first.chunk_id: 0.99})
    result = await system.retrieval.execute("clinic opening", principal())
    assert system.store.calls == [("dense", 20), ("keyword", 20)]
    assert system.reranker.calls[0][0].chunk_id == second.chunk_id
    assert result.selected[0].hit.chunk_id == first.chunk_id
    assert result.citations[0].relevance_score == 0.99


async def test_empty_retrieval_does_not_load_reranker() -> None:
    system = harness()
    result = await system.retrieval.execute("missing", principal())
    assert not result.citations
    assert not system.reranker.calls


async def test_keyword_only_evidence_is_reranked() -> None:
    system = harness(keyword=[hit(1)])
    result = await system.retrieval.execute("clinic", principal())
    assert result.dense_count == 0 and result.keyword_count == 1
    assert result.citations[0].chunk_id == hit(1).chunk_id
    assert len(system.reranker.calls) == 1


async def test_reranker_threshold_is_inclusive_and_not_a_dense_score_threshold() -> None:
    first, second = hit(1, score=0.001), hit(2, score=0.999)
    system = harness([first, second], scores={first.chunk_id: 0.5, second.chunk_id: 0.49})
    result = await system.retrieval.execute("clinic", principal())
    assert [item.chunk_id for item in result.citations] == [first.chunk_id]


@pytest.mark.parametrize(
    "bad_scores",
    [
        [],
        [RerankScore(UUID(int=999), 0.9)],
        [RerankScore(UUID(int=1), 0.9), RerankScore(UUID(int=1), 0.9)],
        [RerankScore(UUID(int=1), float("nan"))],
        [RerankScore(UUID(int=1), float("inf"))],
        [RerankScore(UUID(int=1), 1.1)],
        [RerankScore(UUID(int=1), -0.1)],
    ],
)
async def test_bad_reranker_results_fail_without_rrf_fallback(bad_scores) -> None:
    system = harness([hit(1)])
    system.reranker.override = bad_scores
    with pytest.raises(KnowledgeUnavailableError):
        await system.retrieval.execute("clinic", principal())
    assert system.audit.entries[-1].outcome == "error"


async def test_trace_contains_counts_ranks_all_scores_selection_and_latency() -> None:
    system = harness([hit(1)], [hit(1), hit(2)])
    result = await system.retrieval.execute("clinic", principal())
    entry = system.audit.entries[-1]
    trace = json.loads(entry.detail["telemetry"])
    assert entry.detail["query"] == "clinic"
    assert entry.resource_id == result.trace_id
    assert (trace["dense_count"], trace["keyword_count"], trace["fused_count"]) == (1, 2, 2)
    assert trace["rrf_k"] == 60
    assert trace["reranked"][0]["rrf_score"] == pytest.approx(2 / 61)
    assert trace["reranked"][0]["relevance_score"] == 0.9
    assert trace["selected_chunk_ids"] == [str(hit(1).chunk_id), str(hit(2).chunk_id)]
    assert trace["latency_ms"] >= 0 and trace["refused"] is False


async def test_context_budget_does_not_silently_truncate_a_source() -> None:
    system = harness(
        [hit(1, "a" * 50), hit(2, "short")], options=RetrievalOptions(max_context_characters=10)
    )
    result = await system.retrieval.execute("clinic", principal())
    assert [item.text_snippet for item in result.citations] == ["short"]


@pytest.mark.parametrize("query", ["", "  ", "a" * 2001, "clinic\x00"])
async def test_invalid_query_is_rejected_before_search(query: str) -> None:
    system = harness([hit(1)])
    with pytest.raises(InvariantViolationError):
        await system.retrieval.execute(query, principal())
    assert not system.store.calls
