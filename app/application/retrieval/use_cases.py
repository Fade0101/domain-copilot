"""Dense + PostgreSQL FTS -> RRF -> cross-encoder -> bounded evidence."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import KnowledgeUnavailableError, ProviderError, RetrievalStoreError
from app.application.observability.context import current_trace, trace_scope
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.reranking import IReranker
from app.application.ports.retrieval import IRetrievalStore
from app.application.ports.system import IIdGenerator
from app.application.retrieval.dto import RankedCandidate, RetrievalResult
from app.application.retrieval.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion
from app.application.retrieval.observability import RetrievalObserver
from app.domain.auth.value_objects import Permission
from app.domain.shared.errors import InvariantViolationError

MAX_QUERY_CHARACTERS = 2000


def validate_query(query: str) -> str:
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARACTERS:
        raise InvariantViolationError("Question must contain 1 to 2000 characters")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in query):
        raise InvariantViolationError("Question contains invalid control characters")
    return query


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    candidate_k: int = 20
    top_k: int = 5
    rrf_k: int = DEFAULT_RRF_K
    score_threshold: float = 0.5
    max_context_chunks: int = 12
    max_context_characters: int = 24_000
    timeout_seconds: float = 180.0


class HybridRetrievalUseCase:
    def __init__(
        self,
        store: IRetrievalStore,
        embeddings: IEmbeddingProvider,
        reranker: IReranker,
        authorization: AuthorizationService,
        observer: RetrievalObserver,
        id_generator: IIdGenerator,
        options: RetrievalOptions = RetrievalOptions(),
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._reranker = reranker
        self._authorization = authorization
        self._observer = observer
        self._ids = id_generator
        self._options = options

    async def execute(
        self, query: str, principal: Principal, *, trace_id: str | None = None
    ) -> RetrievalResult:
        trace_id = trace_id or self._ids.new_id()
        parent = current_trace()
        if parent is not None and str(parent.trace_id) == trace_id:
            return await self._execute(query, principal, trace_id)
        with trace_scope(UUID(trace_id), UUID(principal.user_id.value), kind="retrieval"):
            return await self._execute(query, principal, trace_id)

    async def _execute(self, query: str, principal: Principal, trace_id: str) -> RetrievalResult:
        self._authorization.require_permission(principal, Permission.ASK_QUESTION)
        query = validate_query(query)
        started_at = self._observer.clock.now()
        started = perf_counter()
        timings: dict[str, float] = {}
        telemetry: dict[str, object] = {
            "dense_count": 0,
            "keyword_count": 0,
            "fused_count": 0,
            "rrf_k": self._options.rrf_k,
            "reranker_model": self._reranker.model_name,
            "score_threshold": self._options.score_threshold,
            "reranked": [],
            "selected_chunk_ids": [],
            "timings_ms": timings,
        }
        outcome = "error"
        try:
            async with asyncio.timeout(self._options.timeout_seconds):
                phase = perf_counter()
                embedding = await self._embeddings.generate_embeddings([query])
                timings["embedding"] = (perf_counter() - phase) * 1000
                if (
                    len(embedding.vectors) != 1
                    or not embedding.vectors[0]
                    or len(embedding.vectors[0]) != embedding.dimensions
                    or not all(math.isfinite(value) for value in embedding.vectors[0])
                ):
                    raise ProviderError("Embedding provider returned an invalid query vector")
                phase = perf_counter()
                # The store owns independent sessions, so both read paths can run
                # together. Both must succeed; no silent dense-only fallback.
                results = await asyncio.gather(
                    self._store.dense_search(embedding.vectors[0], top_k=self._options.candidate_k),
                    self._store.keyword_search(query, top_k=self._options.candidate_k),
                    return_exceptions=True,
                )
                dense, keyword = results
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                assert isinstance(dense, list) and isinstance(keyword, list)
                telemetry.update(dense_count=len(dense), keyword_count=len(keyword))
                timings["search"] = (perf_counter() - phase) * 1000
                phase = perf_counter()
                fused = reciprocal_rank_fusion(dense, keyword, k=self._options.rrf_k)
                telemetry["fused_count"] = len(fused)
                timings["fusion"] = (perf_counter() - phase) * 1000
                phase = perf_counter()
                scores = (
                    await self._reranker.rerank(query, [item.hit for item in fused])
                    if fused
                    else []
                )
                timings["reranking"] = (perf_counter() - phase) * 1000
                by_id = {item.chunk_id: item.score for item in scores}
                if (
                    len(by_id) != len(scores)
                    or set(by_id) != {item.hit.chunk_id for item in fused}
                    or any(
                        not math.isfinite(score) or not 0 <= score <= 1 for score in by_id.values()
                    )
                ):
                    raise ProviderError("Reranker returned invalid candidate scores")
                ranked = sorted(
                    [RankedCandidate(item, by_id[item.hit.chunk_id]) for item in fused],
                    key=lambda item: (
                        -item.relevance_score,
                        -item.candidate.rrf_score,
                        str(item.hit.chunk_id),
                    ),
                )
                selected: list[RankedCandidate] = []
                used_characters = 0
                for item in ranked:
                    if len(selected) >= min(self._options.top_k, self._options.max_context_chunks):
                        break
                    if (
                        item.relevance_score < self._options.score_threshold
                        or not item.hit.snippet.strip()
                    ):
                        continue
                    if (
                        used_characters + len(item.hit.snippet)
                        > self._options.max_context_characters
                    ):
                        continue
                    selected.append(item)
                    used_characters += len(item.hit.snippet)
                telemetry["reranked"] = [
                    {
                        "chunk_id": str(item.hit.chunk_id),
                        "document_id": str(item.hit.document_id),
                        "dense_rank": item.candidate.dense_rank,
                        "keyword_rank": item.candidate.keyword_rank,
                        "dense_score": item.candidate.dense_score,
                        "keyword_score": item.candidate.keyword_score,
                        "rrf_score": item.candidate.rrf_score,
                        "relevance_score": item.relevance_score,
                    }
                    for item in ranked
                ]
                telemetry["selected_chunk_ids"] = [str(item.hit.chunk_id) for item in selected]
                telemetry["refused"] = not selected
                outcome = "refused" if not selected else "completed"
                return RetrievalResult(
                    trace_id, tuple(ranked), tuple(selected), len(dense), len(keyword), len(fused)
                )
        except (TimeoutError, ProviderError, RetrievalStoreError) as exc:
            raise KnowledgeUnavailableError("Hybrid retrieval is unavailable") from exc
        finally:
            telemetry["latency_ms"] = (perf_counter() - started) * 1000
            await self._observer.record(
                action="retrieval.hybrid",
                principal=principal,
                trace_id=trace_id,
                query=query,
                started_at=started_at,
                telemetry=telemetry,
                outcome=outcome,
            )
