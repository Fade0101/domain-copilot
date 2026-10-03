"""Framework-free ranked evidence and the exact AC-2.5 citation contract."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.ports.retrieval import SearchHit


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    hit: SearchHit
    rrf_score: float
    dense_rank: int | None
    keyword_rank: int | None
    dense_score: float | None
    keyword_score: float | None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: FusedCandidate
    relevance_score: float

    @property
    def hit(self) -> SearchHit:
        return self.candidate.hit


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: UUID
    document_name: str
    section: str | None
    page: int | None
    chunk_id: UUID
    relevance_score: float
    text_snippet: str

    @classmethod
    def from_candidate(cls, candidate: RankedCandidate) -> Citation:
        hit = candidate.hit
        return cls(
            document_id=hit.document_id,
            document_name=hit.document_name,
            section=hit.section,
            page=hit.page,
            chunk_id=hit.chunk_id,
            relevance_score=candidate.relevance_score,
            text_snippet=hit.snippet,
        )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    trace_id: str
    ranked: tuple[RankedCandidate, ...]
    selected: tuple[RankedCandidate, ...]
    dense_count: int
    keyword_count: int
    fused_count: int

    @property
    def citations(self) -> tuple[Citation, ...]:
        return tuple(Citation.from_candidate(candidate) for candidate in self.selected)
