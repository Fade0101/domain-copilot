"""Port: durable retrieval store for hybrid search (BRD AC-2.1; SDD A.5.5, B.1.2).

Declares the dense (vector) and keyword (full-text) search operations that the
retrieval pipeline depends on, plus the records that cross the boundary. Dense
and keyword search are deliberately *separate* methods: BRD AC-2.1 fuses two
independently-ranked result lists, so the fusion step (Reciprocal Rank Fusion)
needs each ranking on its own. Fusion, re-ranking, evidence thresholds and
refusal are **not** part of this port -- they are orchestration concerns owned by
the hybrid-retrieval ticket.

SDK-free by construction: no pgvector, SQLAlchemy or psycopg type leaks through
this interface, so the concrete store can be swapped (pgvector -> a dedicated
vector database) without touching domain or application code. Embeddings cross
the boundary as plain ``list[float]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """A chunk and its embedding, as handed to the store for indexing.

    Carries both the citation metadata (``section``, ``page``) that BRD AC-2.5
    requires on every answer and the embedding provenance
    (``embedding_model``, ``embedding_dim``, ``embedding_version``) that makes a
    stored vector interpretable. Provenance travels *with* the vector because
    distances are only meaningful between embeddings from the same model and
    version, and because re-embedding the corpus under a new model must not
    silently overwrite the old vectors.
    """

    chunk_id: UUID
    document_id: UUID
    text: str
    embedding: list[float]
    embedding_model: str
    embedding_dim: int
    embedding_version: str
    section: str | None
    page: int | None
    token_count: int | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked search result, carrying everything a citation needs.

    The fields mirror the citation object in BRD AC-2.5 so a caller can build a
    citation without a second lookup. ``score`` is comparable *within* one
    ranking only -- cosine similarity for dense hits, ``ts_rank`` for keyword
    hits -- which is precisely why fusion happens a layer up.

    The three embedding provenance fields are ``None`` for keyword hits: a
    full-text match does not involve an embedding, so claiming one would be
    fabricated metadata.
    """

    document_id: UUID
    chunk_id: UUID
    document_name: str
    section: str | None
    page: int | None
    snippet: str
    score: float
    embedding_model: str | None
    embedding_dim: int | None
    embedding_version: str | None


@runtime_checkable
class IRetrievalStore(Protocol):
    """Durable dense + keyword index over document chunks."""

    async def upsert_chunks(self, records: list[ChunkRecord]) -> None:
        """Index ``records``, replacing any existing rows for the same keys.

        Idempotent: re-indexing the same chunk under the same embedding model and
        version updates in place rather than creating a duplicate, so ingestion
        can be retried safely.
        """
        ...

    async def dense_search(self, embedding: list[float], *, top_k: int) -> list[SearchHit]:
        """Return the ``top_k`` chunks most similar to ``embedding``.

        Ranked by cosine similarity, highest first. Each hit reports the
        provenance of the *stored* vector it matched.
        """
        ...

    async def keyword_search(self, query: str, *, top_k: int) -> list[SearchHit]:
        """Return the ``top_k`` chunks matching ``query`` by full-text search.

        Ranked by full-text relevance, highest first. Only chunks that actually
        match are returned, and the embedding provenance fields are ``None``.
        """
        ...
