"""Shared evidence registry for pipeline agents.

Maintains verified, non-quarantined citations derived strictly from real tool
executions. The LLM is NEVER trusted as a source of citation identity.
"""

from __future__ import annotations

from uuid import UUID

from app.application.retrieval.dto import Citation


class VerifiedEvidenceRegistry:
    """Internal registry storing verified, non-quarantined citations from tool results.

    Only citations returned by real tool executions and passing instruction-signal
    checks are eligible to enter agent findings, flags, or verdicts.
    """

    def __init__(self) -> None:
        self._by_chunk_id: dict[UUID, Citation] = {}
        self._ordered: list[Citation] = []

    def register(self, citation: Citation) -> None:
        if citation.chunk_id not in self._by_chunk_id:
            self._by_chunk_id[citation.chunk_id] = citation
            self._ordered.append(citation)

    def contains(self, chunk_id: UUID) -> bool:
        return chunk_id in self._by_chunk_id

    def get(self, chunk_id: UUID) -> Citation | None:
        return self._by_chunk_id.get(chunk_id)

    def all_citations(self) -> tuple[Citation, ...]:
        return tuple(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)
