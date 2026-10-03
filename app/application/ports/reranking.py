"""Replaceable cross-encoder boundary. Models return scores, never citation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.application.ports.retrieval import SearchHit


@dataclass(frozen=True, slots=True)
class RerankScore:
    chunk_id: UUID
    score: float


@runtime_checkable
class IReranker(Protocol):
    @property
    def model_name(self) -> str: ...

    async def rerank(self, query: str, candidates: list[SearchHit]) -> list[RerankScore]:
        """Return exactly one finite [0, 1] relevance score per input chunk.

        Scores are sigmoid-normalized cross-encoder logits, not calibrated
        probabilities of correctness. Missing/duplicate/foreign IDs are errors.
        """
        ...
