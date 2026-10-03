"""Reciprocal Rank Fusion: sum(1 / (k + one_based_rank)), with k=60 by default."""

from __future__ import annotations

import math
from uuid import UUID

from app.application.errors import RetrievalStoreError
from app.application.ports.retrieval import SearchHit
from app.application.retrieval.dto import FusedCandidate

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    dense: list[SearchHit], keyword: list[SearchHit], *, k: int = DEFAULT_RRF_K
) -> list[FusedCandidate]:
    """Fuse independent rankings, counting each chunk once per source.

    Input order is rank order. Ties in fused scores break on chunk UUID. Dense
    and keyword raw scores are never added or compared with one another.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("RRF k must be a positive integer")
    sources: list[dict[UUID, tuple[int, SearchHit]]] = []
    for hits in (dense, keyword):
        ranking: dict[UUID, tuple[int, SearchHit]] = {}
        for hit in hits:
            if not math.isfinite(hit.score):
                raise RetrievalStoreError("Retrieval returned an invalid score")
            if hit.chunk_id not in ranking:
                ranking[hit.chunk_id] = (len(ranking) + 1, hit)
        sources.append(ranking)
    dense_ranks, keyword_ranks = sources
    fused: list[FusedCandidate] = []
    for identifier in dense_ranks.keys() | keyword_ranks.keys():
        d = dense_ranks.get(identifier)
        s = keyword_ranks.get(identifier)
        hit = d[1] if d else keyword_ranks[identifier][1]
        if d and s:
            other = s[1]
            if (hit.document_id, hit.document_name, hit.section, hit.page, hit.snippet) != (
                other.document_id,
                other.document_name,
                other.section,
                other.page,
                other.snippet,
            ):
                # Concurrent reindexing must not produce a composite citation
                # whose text and source came from different versions.
                raise RetrievalStoreError("Retrieval returned inconsistent citation metadata")
        fused.append(
            FusedCandidate(
                hit=hit,
                rrf_score=(1.0 / (k + d[0]) if d else 0.0) + (1.0 / (k + s[0]) if s else 0.0),
                dense_rank=d[0] if d else None,
                keyword_rank=s[0] if s else None,
                dense_score=d[1].score if d else None,
                keyword_score=s[1].score if s else None,
            )
        )
    return sorted(fused, key=lambda item: (-item.rrf_score, str(item.hit.chunk_id)))
