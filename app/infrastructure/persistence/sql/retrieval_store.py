"""PostgreSQL + pgvector implementation of :class:`IRetrievalStore` (BRD AC-2.1).

Provides the two halves of hybrid retrieval as independent operations:

* :meth:`PostgresRetrievalStore.dense_search` -- pgvector cosine distance over
  ``chunk_embeddings``, accelerated by the HNSW index.
* :meth:`PostgresRetrievalStore.keyword_search` -- PostgreSQL full-text search
  over the stored generated ``chunks.content_tsv``, scored with ``ts_rank`` and
  accelerated by the GIN index.

Fusion (RRF), re-ranking, evidence thresholds and refusal are **not** here: this
adapter returns two independently-ranked lists and nothing more, so the
orchestration ticket owns how they are combined.

Every SQLAlchemy/asyncpg exception is translated into the
:class:`~app.application.errors.RetrievalStoreError` family before it leaves a
method, so no driver type crosses the application boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError, TimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import RetrievalStoreError, RetrievalStoreUnavailableError
from app.application.ports.retrieval import ChunkRecord, IRetrievalStore, SearchHit
from app.infrastructure.persistence.sql import tables

#: Text-search configuration used for both indexing and querying. It must match
#: the configuration baked into the generated ``content_tsv`` column, or the GIN
#: index would be searched with a different stemmer than it was built with.
_TEXT_SEARCH_CONFIG = "english"

#: Generic messages. The underlying exception is chained via ``raise ... from``
#: (so it reaches the logs) but never interpolated into the message, because the
#: application-error HTTP handler echoes ``str(exc)`` to the caller and database
#: internals must not leak (SDD A.5.1).
_UNAVAILABLE_MESSAGE = "Retrieval store is unavailable"
_QUERY_FAILED_MESSAGE = "Retrieval store query failed"


def _tsquery(query: str) -> sa.Function[Any]:
    """Build a ``plainto_tsquery`` over ``query``.

    ``plainto_tsquery`` takes plain user text -- it escapes and tokenises it
    rather than parsing operators -- and the text is passed as a bound
    parameter, so a query string can never alter the SQL. The configuration is
    cast to ``regconfig`` because PostgreSQL resolves the two-argument overload
    on that type.
    """
    return sa.func.plainto_tsquery(
        sa.cast(sa.literal(_TEXT_SEARCH_CONFIG), postgresql.REGCONFIG),
        query,
    )


class PostgresRetrievalStore(IRetrievalStore):
    """Durable dense + keyword index backed by PostgreSQL and pgvector.

    The embedding provenance passed to the constructor describes the vectors
    this instance *writes*, and scopes the vectors it *reads* in
    :meth:`dense_search`.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        embedding_model: str,
        embedding_dim: int,
        embedding_version: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._embedding_model = embedding_model
        self._embedding_dim = embedding_dim
        self._embedding_version = embedding_version

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, translating database failures into typed errors."""
        try:
            async with self._sessionmaker() as session:
                yield session
        except (OperationalError, InterfaceError, TimeoutError) as exc:
            # Connection/pool level: the query was never answered, so a caller
            # may retry it.
            raise RetrievalStoreUnavailableError(_UNAVAILABLE_MESSAGE) from exc
        except SQLAlchemyError as exc:
            raise RetrievalStoreError(_QUERY_FAILED_MESSAGE) from exc

    def _validate(self, records: Sequence[ChunkRecord]) -> None:
        """Reject provenance/dimension mismatches before touching the database.

        A wrong-length vector would otherwise fail deep inside the driver with a
        message about the column type; failing here names the offending chunk.
        """
        for record in records:
            if record.embedding_dim != self._embedding_dim:
                raise RetrievalStoreError(
                    f"Chunk {record.chunk_id} declares embedding_dim "
                    f"{record.embedding_dim}, but this store indexes "
                    f"{self._embedding_dim}-dimensional vectors."
                )
            if len(record.embedding) != self._embedding_dim:
                raise RetrievalStoreError(
                    f"Chunk {record.chunk_id} has a {len(record.embedding)}-dimensional "
                    f"embedding, but declares embedding_dim {record.embedding_dim}."
                )

    async def upsert_chunks(self, records: list[ChunkRecord]) -> None:
        """Index ``records`` idempotently (see :class:`IRetrievalStore`)."""
        if not records:
            return

        self._validate(records)

        chunk_rows: list[dict[str, Any]] = [
            {
                "id": record.chunk_id,
                "document_id": record.document_id,
                "text": record.text,
                # chunks.metadata is NOT NULL with no server default (owned by
                # the persistence ticket). Seed it on insert; never overwrite an
                # existing value on conflict.
                "metadata": {},
                "section": record.section,
                "page": record.page,
                "token_count": record.token_count,
                "document_version": record.document_version,
                "ingested_at": record.ingested_at,
            }
            for record in records
        ]
        embedding_rows: list[dict[str, Any]] = [
            {
                "id": uuid4(),
                "chunk_id": record.chunk_id,
                "embedding": record.embedding,
                "embedding_model": record.embedding_model,
                "embedding_dim": record.embedding_dim,
                "embedding_version": record.embedding_version,
            }
            for record in records
        ]

        chunk_stmt = pg_insert(tables.chunks).values(chunk_rows)
        chunk_stmt = chunk_stmt.on_conflict_do_update(
            index_elements=[tables.chunks.c.id],
            set_={
                "document_id": chunk_stmt.excluded.document_id,
                "text": chunk_stmt.excluded.text,
                "section": chunk_stmt.excluded.section,
                "page": chunk_stmt.excluded.page,
                "token_count": chunk_stmt.excluded.token_count,
                "document_version": chunk_stmt.excluded.document_version,
                "ingested_at": chunk_stmt.excluded.ingested_at,
            },
        )

        embedding_stmt = pg_insert(tables.chunk_embeddings).values(embedding_rows)
        embedding_stmt = embedding_stmt.on_conflict_do_update(
            # Keyed on (chunk_id, embedding_model, embedding_version): re-running
            # ingestion refreshes the vector in place, while re-embedding under a
            # different model or version inserts a new row alongside it.
            constraint=tables.CHUNK_EMBEDDING_PROVENANCE_CONSTRAINT,
            set_={
                "embedding": embedding_stmt.excluded.embedding,
                "embedding_dim": embedding_stmt.excluded.embedding_dim,
                "created_at": sa.func.now(),
            },
        )

        async with self._session() as session:
            # One transaction: a chunk is never visible without its embedding.
            async with session.begin():
                await session.execute(chunk_stmt)
                await session.execute(embedding_stmt)

    async def dense_search(self, embedding: list[float], *, top_k: int) -> list[SearchHit]:
        """Return the ``top_k`` nearest chunks by cosine similarity."""
        if len(embedding) != self._embedding_dim:
            raise RetrievalStoreError(
                f"Query embedding has {len(embedding)} dimensions, but this store "
                f"indexes {self._embedding_dim}-dimensional vectors."
            )

        # `<=>` is pgvector's cosine distance, which the HNSW index on
        # vector_cosine_ops serves. Ascending distance = descending similarity.
        distance = tables.chunk_embeddings.c.embedding.cosine_distance(embedding)

        stmt = (
            sa.select(
                tables.chunks.c.document_id,
                tables.chunks.c.id.label("chunk_id"),
                tables.documents.c.filename,
                tables.chunks.c.section,
                tables.chunks.c.page,
                tables.chunks.c.text,
                distance.label("distance"),
                tables.chunks.c.document_version,
                tables.chunks.c.ingested_at,
                tables.chunk_embeddings.c.embedding_model,
                tables.chunk_embeddings.c.embedding_dim,
                tables.chunk_embeddings.c.embedding_version,
            )
            .select_from(
                tables.chunk_embeddings.join(
                    tables.chunks,
                    tables.chunk_embeddings.c.chunk_id == tables.chunks.c.id,
                ).join(
                    tables.documents,
                    tables.chunks.c.document_id == tables.documents.c.id,
                )
            )
            # Vectors from different models/versions occupy different spaces, so
            # comparing distances across them is meaningless. The uniqueness key
            # deliberately allows several provenances per chunk, which makes this
            # filter load-bearing rather than defensive.
            .where(
                tables.chunk_embeddings.c.embedding_model == self._embedding_model,
                tables.chunk_embeddings.c.embedding_version == self._embedding_version,
            )
            .order_by(distance)
            .limit(top_k)
        )

        async with self._session() as session:
            rows = (await session.execute(stmt)).all()

        return [
            SearchHit(
                document_id=row.document_id,
                chunk_id=row.chunk_id,
                document_name=row.filename,
                section=row.section,
                page=row.page,
                snippet=row.text,
                # Cosine similarity: pgvector's distance is 1 - similarity.
                score=1.0 - float(row.distance),
                document_version=row.document_version,
                ingested_at=row.ingested_at,
                # Provenance is read back from the stored row, never assumed from
                # this instance's configuration -- a citation must describe the
                # vector that actually matched.
                embedding_model=row.embedding_model,
                embedding_dim=row.embedding_dim,
                embedding_version=row.embedding_version,
            )
            for row in rows
        ]

    async def keyword_search(self, query: str, *, top_k: int) -> list[SearchHit]:
        """Return the ``top_k`` chunks matching ``query`` by full-text search."""
        tsquery = _tsquery(query)
        rank = sa.func.ts_rank(tables.chunks.c.content_tsv, tsquery)

        stmt = (
            sa.select(
                tables.chunks.c.document_id,
                tables.chunks.c.id.label("chunk_id"),
                tables.documents.c.filename,
                tables.chunks.c.section,
                tables.chunks.c.page,
                tables.chunks.c.text,
                rank.label("rank"),
                tables.chunks.c.document_version,
                tables.chunks.c.ingested_at,
            )
            .select_from(
                tables.chunks.join(
                    tables.documents,
                    tables.chunks.c.document_id == tables.documents.c.id,
                )
            )
            # `@@` restricts to genuine matches, so a non-matching chunk is never
            # returned with a zero score.
            .where(tables.chunks.c.content_tsv.bool_op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )

        async with self._session() as session:
            rows = (await session.execute(stmt)).all()

        return [
            SearchHit(
                document_id=row.document_id,
                chunk_id=row.chunk_id,
                document_name=row.filename,
                section=row.section,
                page=row.page,
                snippet=row.text,
                score=float(row.rank),
                document_version=row.document_version,
                ingested_at=row.ingested_at,
                # A full-text match involves no embedding, so reporting one would
                # be fabricated provenance.
                embedding_model=None,
                embedding_dim=None,
                embedding_version=None,
            )
            for row in rows
        ]
