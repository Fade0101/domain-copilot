"""Retrieval against real PostgreSQL + pgvector (BRD AC-2.1; SDD A.5.5).

Nothing here is faked: cosine ordering comes from pgvector's ``<=>`` operator and
keyword ranking from PostgreSQL's ``ts_rank`` over the stored generated
``tsvector`` column. Each run creates a scratch database, migrates it to head and
drops it afterwards, so a developer's own data is never touched -- the same
pattern as ``test_sql_ownership.py``.

Skipped when no database is reachable so the suite still runs without one, but
**not** in CI: CI provisions pgvector, and a silent skip there would retire the
only tests proving the retrieval adapter works against a real database.

``TEST_DATABASE_URL`` overrides the connection; the default matches
``docker-compose.yml``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import RetrievalStoreError
from app.application.ports.retrieval import ChunkRecord, IRetrievalStore
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore
from app.infrastructure.persistence.sql.tables import EMBEDDING_DIM

_ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
_SCRATCH_DB = "dc_ticket9_retrieval_test"

_MODEL = "all-MiniLM-L6-v2"
_VERSION = "1"

#: Tables this module owns and truncates between tests. ``chunks`` cascades into
#: ``chunk_embeddings``; ``documents`` is included because every test seeds its
#: own document to join against for citations.
_OWNED_TABLES = "chunk_embeddings, chunks, documents"


def _running_in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def _database_reachable() -> bool:
    async def probe() -> bool:
        try:
            connection = await asyncio.wait_for(asyncpg.connect(_ADMIN_URL), timeout=3)
        except Exception:
            return False
        await connection.close()
        return True

    try:
        return asyncio.run(probe())
    except Exception:
        return False


_REACHABLE = _database_reachable()

# Skip locally, but never in CI: there the service is guaranteed, so an
# unreachable database is a real failure and the fixture below says so.
pytestmark = pytest.mark.skipif(
    not _REACHABLE and not _running_in_ci(),
    reason=f"no PostgreSQL reachable at {_ADMIN_URL.rsplit('@', 1)[-1]}",
)


@contextmanager
def _preserving_logging() -> Iterator[None]:
    """Restore logger state around an in-process Alembic run.

    Mirrors ``test_sql_ownership._preserving_logging``: ``fileConfig`` can disable
    every existing logger, which would leak out of this module and break
    unrelated ``caplog`` assertions.
    """
    manager = logging.Logger.manager
    before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        yield
    finally:
        for name, logger in manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.disabled = before.get(name, False)


def _admin_sql(statement: str) -> None:
    async def run() -> None:
        connection = await asyncpg.connect(_ADMIN_URL)
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def _scratch_url() -> str:
    base, _, _ = _ADMIN_URL.rpartition("/")
    return f"{base}/{_SCRATCH_DB}"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A scratch database migrated to head, dropped on the way out."""
    if not _REACHABLE:
        # Only reachable in CI, where skipping is disallowed.
        pytest.fail(
            f"No PostgreSQL reachable at {_ADMIN_URL.rsplit('@', 1)[-1]}. "
            "CI must provide a pgvector service; these tests must not be skipped here."
        )

    _admin_sql(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
    _admin_sql(f'CREATE DATABASE "{_SCRATCH_DB}"')
    try:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("DATABASE__URL", _scratch_url())
        try:
            # env.py resolves the URL from DATABASE__URL and calls asyncio.run
            # itself, so this fixture must stay synchronous.
            with _preserving_logging():
                command.upgrade(Config("alembic.ini"), "head")
        finally:
            monkeypatch.undo()
        yield _scratch_url()
    finally:
        _admin_sql(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')


@pytest.fixture
async def session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory on the scratch database, with owned tables emptied."""
    database = Database(migrated_database)
    try:
        async with database.session_factory() as session:
            await session.execute(text(f"TRUNCATE {_OWNED_TABLES} RESTART IDENTITY CASCADE"))
            await session.commit()
        yield database.session_factory
    finally:
        await database.dispose()


@pytest.fixture
async def seeded_document(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str]:
    """Insert one document and return ``(id, filename)`` for citation assertions.

    ``documents.user_id`` is NOT NULL and references ``users``, so a user row is
    seeded too -- the real foreign keys are exercised, not bypassed.
    """
    user_id = uuid4()
    document_id = uuid4()
    filename = "clinical-guideline.pdf"
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, role, created_at) "
                "VALUES (:id, :email, 'x', 'analyst', now())"
            ),
            {"id": user_id, "email": f"retrieval-{user_id}@example.test"},
        )
        await session.execute(
            text(
                "INSERT INTO documents (id, user_id, filename, content, status, metadata) "
                "VALUES (:id, :user_id, :filename, :content, 'COMPLETED', '{}'::jsonb)"
            ),
            {
                "id": document_id,
                "user_id": user_id,
                "filename": filename,
                "content": "seeded by the retrieval integration suite",
            },
        )
        await session.commit()
    return document_id, filename


def _unit_vector(axis: int) -> list[float]:
    """A one-hot vector, so cosine similarity against another axis is exactly 0."""
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


def _blend(primary: int, secondary: int, weight: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[primary] = 1.0
    vector[secondary] = weight
    return vector


def _store(
    session_factory: async_sessionmaker[AsyncSession], *, version: str = _VERSION
) -> PostgresRetrievalStore:
    return PostgresRetrievalStore(
        session_factory,
        embedding_model=_MODEL,
        embedding_dim=EMBEDDING_DIM,
        embedding_version=version,
    )


def _record(
    document_id: UUID,
    text_value: str,
    embedding: list[float],
    *,
    section: str | None = "Dosage",
    page: int | None = 4,
    token_count: int | None = 11,
    chunk_id: UUID | None = None,
    version: str = _VERSION,
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id or uuid4(),
        document_id=document_id,
        text=text_value,
        embedding=embedding,
        embedding_model=_MODEL,
        embedding_dim=len(embedding),
        embedding_version=version,
        section=section,
        page=page,
        token_count=token_count,
    )


async def _count(session_factory: async_sessionmaker[AsyncSession], table: str) -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        return int(result.scalar_one())


async def test_store_satisfies_the_port(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert isinstance(_store(session_factory), IRetrievalStore)


async def test_dense_search_orders_by_cosine_similarity(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)

    exact = _record(document_id, "exact axis match", _unit_vector(0))
    partial = _record(document_id, "partial overlap", _blend(0, 1, 1.0))
    orthogonal = _record(document_id, "orthogonal", _unit_vector(1))
    await store.upsert_chunks([orthogonal, partial, exact])

    hits = await store.dense_search(_unit_vector(0), top_k=3)

    assert [hit.chunk_id for hit in hits] == [exact.chunk_id, partial.chunk_id, orthogonal.chunk_id]
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    # Exact match -> 1; 45-degree blend -> 1/sqrt(2); orthogonal -> 0.
    assert scores[0] == pytest.approx(1.0, abs=1e-5)
    assert scores[1] == pytest.approx(0.7071, abs=1e-3)
    assert scores[2] == pytest.approx(0.0, abs=1e-5)


async def test_dense_scores_lie_in_the_cosine_similarity_range(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    opposite = [0.0] * EMBEDDING_DIM
    opposite[0] = -1.0
    await store.upsert_chunks(
        [
            _record(document_id, "aligned", _unit_vector(0)),
            _record(document_id, "opposed", opposite),
        ]
    )

    hits = await store.dense_search(_unit_vector(0), top_k=2)

    assert all(-1.0 - 1e-6 <= hit.score <= 1.0 + 1e-6 for hit in hits)
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[-1].score == pytest.approx(-1.0, abs=1e-5)


async def test_dense_search_preserves_citation_metadata_and_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, filename = seeded_document
    store = _store(session_factory)
    record = _record(
        document_id,
        "aspirin 75 mg once daily",
        _unit_vector(0),
        section="Interactions",
        page=12,
        token_count=6,
    )
    await store.upsert_chunks([record])

    hit = (await store.dense_search(_unit_vector(0), top_k=1))[0]

    assert hit.document_id == document_id
    assert hit.chunk_id == record.chunk_id
    assert hit.document_name == filename
    assert hit.section == "Interactions"
    assert hit.page == 12
    assert hit.snippet == "aspirin 75 mg once daily"
    # Provenance must be read back from the stored embedding row.
    assert hit.embedding_model == _MODEL
    assert hit.embedding_dim == EMBEDDING_DIM
    assert hit.embedding_version == _VERSION


async def test_dense_search_ignores_other_model_versions(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    """Vectors from another model/version occupy a different space, so they must
    never be ranked against this store's query embedding."""
    document_id, _ = seeded_document
    store = _store(session_factory)
    mine = _record(document_id, "current version", _unit_vector(0))
    other = _record(document_id, "older version", _unit_vector(0), version="0")
    await store.upsert_chunks([mine, other])

    hits = await store.dense_search(_unit_vector(0), top_k=10)

    assert [hit.chunk_id for hit in hits] == [mine.chunk_id]
    assert all(hit.embedding_version == _VERSION for hit in hits)


async def test_keyword_search_uses_ts_rank_and_full_text_matching(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    strong = _record(
        document_id, "aspirin dosage: aspirin is given as aspirin 75 mg", _unit_vector(0)
    )
    weak = _record(document_id, "aspirin appears once here", _unit_vector(1))
    await store.upsert_chunks([strong, weak])

    hits = await store.keyword_search("aspirin", top_k=5)

    assert [hit.chunk_id for hit in hits] == [strong.chunk_id, weak.chunk_id]
    assert hits[0].score > hits[1].score

    # Cross-check the score really is PostgreSQL's ts_rank over content_tsv.
    async with session_factory() as session:
        expected: float = (
            await session.execute(
                text(
                    "SELECT ts_rank(content_tsv, plainto_tsquery('english', :q)) "
                    "FROM chunks WHERE id = :id"
                ),
                {"q": "aspirin", "id": strong.chunk_id},
            )
        ).scalar_one()
    assert hits[0].score == pytest.approx(float(expected), abs=1e-6)


async def test_keyword_search_applies_english_stemming(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    """Proof this is PostgreSQL FTS and not a substring match: a stemmed
    inflection matches, and a word merely sharing a prefix does not."""
    document_id, _ = seeded_document
    store = _store(session_factory)
    await store.upsert_chunks(
        [_record(document_id, "monitor for bleeding complications", _unit_vector(0))]
    )

    assert await store.keyword_search("bleed", top_k=5)
    assert not await store.keyword_search("complicated machinery", top_k=5)


async def test_keyword_search_returns_only_matching_chunks(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    match = _record(document_id, "aspirin dosage guidance", _unit_vector(0))
    miss = _record(document_id, "unrelated administrative appendix", _unit_vector(1))
    await store.upsert_chunks([match, miss])

    hits = await store.keyword_search("aspirin", top_k=10)

    assert [hit.chunk_id for hit in hits] == [match.chunk_id]


async def test_keyword_hits_carry_citations_but_no_embedding_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, filename = seeded_document
    store = _store(session_factory)
    record = _record(
        document_id,
        "warfarin interaction warning",
        _unit_vector(0),
        section="Contraindications",
        page=31,
    )
    await store.upsert_chunks([record])

    hit = (await store.keyword_search("warfarin", top_k=1))[0]

    assert hit.document_id == document_id
    assert hit.chunk_id == record.chunk_id
    assert hit.document_name == filename
    assert hit.section == "Contraindications"
    assert hit.page == 31
    assert hit.snippet == "warfarin interaction warning"
    assert hit.embedding_model is None
    assert hit.embedding_dim is None
    assert hit.embedding_version is None


async def test_dense_and_keyword_search_are_independently_callable(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    """The fusion ticket combines these two lists; #9 only guarantees each one
    stands alone, in either order."""
    document_id, _ = seeded_document
    store = _store(session_factory)
    record = _record(document_id, "aspirin dosage guidance", _unit_vector(0))
    await store.upsert_chunks([record])

    keyword_first = await store.keyword_search("aspirin", top_k=5)
    dense_after = await store.dense_search(_unit_vector(0), top_k=5)
    dense_alone = await store.dense_search(_unit_vector(0), top_k=5)

    assert [hit.chunk_id for hit in keyword_first] == [record.chunk_id]
    assert [hit.chunk_id for hit in dense_after] == [record.chunk_id]
    assert [hit.chunk_id for hit in dense_alone] == [record.chunk_id]


async def test_repeated_upsert_does_not_duplicate_rows(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    record = _record(document_id, "aspirin dosage guidance", _unit_vector(0))

    await store.upsert_chunks([record])
    await store.upsert_chunks([record])
    await store.upsert_chunks([record])

    assert await _count(session_factory, "chunks") == 1
    assert await _count(session_factory, "chunk_embeddings") == 1


async def test_reembedding_under_a_new_version_adds_a_row(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    """The uniqueness key includes model and version precisely so a re-embed does
    not destroy the vectors the live index is still serving."""
    document_id, _ = seeded_document
    chunk_id = uuid4()
    v1 = _store(session_factory)
    await v1.upsert_chunks([_record(document_id, "same chunk", _unit_vector(0), chunk_id=chunk_id)])

    v2 = _store(session_factory, version="2")
    await v2.upsert_chunks(
        [_record(document_id, "same chunk", _unit_vector(1), chunk_id=chunk_id, version="2")]
    )

    assert await _count(session_factory, "chunks") == 1
    assert await _count(session_factory, "chunk_embeddings") == 2
    hits = await v2.dense_search(_unit_vector(1), top_k=5)
    assert [hit.embedding_version for hit in hits] == ["2"]


async def test_upsert_updates_text_and_citation_metadata_in_place(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    chunk_id = uuid4()
    await store.upsert_chunks(
        [
            _record(
                document_id,
                "original text",
                _unit_vector(0),
                chunk_id=chunk_id,
                section="Old",
                page=1,
            )
        ]
    )
    await store.upsert_chunks(
        [
            _record(
                document_id,
                "revised aspirin text",
                _unit_vector(0),
                chunk_id=chunk_id,
                section="New",
                page=2,
            )
        ]
    )

    hit = (await store.dense_search(_unit_vector(0), top_k=1))[0]
    assert hit.snippet == "revised aspirin text"
    assert hit.section == "New"
    assert hit.page == 2
    # The generated tsvector must track the updated text, not the original.
    assert [h.chunk_id for h in await store.keyword_search("revised", top_k=5)] == [chunk_id]
    assert not await store.keyword_search("original", top_k=5)


async def test_wrong_dimension_is_rejected_before_reaching_the_database(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)

    with pytest.raises(RetrievalStoreError):
        await store.upsert_chunks([_record(document_id, "too short", [0.1, 0.2, 0.3])])

    with pytest.raises(RetrievalStoreError):
        await store.dense_search([0.1, 0.2, 0.3], top_k=1)

    assert await _count(session_factory, "chunks") == 0


async def test_empty_upsert_is_a_no_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _store(session_factory).upsert_chunks([])


async def test_hnsw_and_gin_indexes_exist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Migration-managed indexes, asserted by definition rather than name alone."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename IN ('chunks', 'chunk_embeddings')"
                )
            )
        ).all()
    definitions = {row.indexname: row.indexdef for row in rows}

    hnsw = definitions["ix_chunk_embeddings_embedding_hnsw"]
    assert "USING hnsw" in hnsw
    assert "vector_cosine_ops" in hnsw
    # IVFFlat would need training data and so would not be reproducible from a
    # clean database.
    assert "ivfflat" not in hnsw.lower()

    assert "USING gin" in definitions["ix_chunks_content_tsv"]
    assert "content_tsv" in definitions["ix_chunks_content_tsv"]


async def test_content_tsv_is_a_stored_generated_column(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT is_generated, generation_expression "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'chunks' AND column_name = 'content_tsv'"
                )
            )
        ).one()
    assert row.is_generated == "ALWAYS"
    assert "to_tsvector" in row.generation_expression


async def test_embedding_column_matches_the_configured_dimension(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guards against the 1536-vs-384 mismatch this migration exists to correct."""
    async with session_factory() as session:
        typmod: int = (
            await session.execute(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding'"
                )
            )
        ).scalar_one()
    assert int(typmod) == EMBEDDING_DIM


async def test_legacy_embedding_column_is_gone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        present: int = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'chunks' AND column_name = 'embedding'"
                )
            )
        ).scalar_one()
    assert int(present) == 0


async def test_schema_is_at_the_expected_migration_head(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The suite is meaningless if it ran against a stale schema."""
    async with session_factory() as session:
        version: str = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [version]


async def test_deleting_a_chunk_cascades_to_its_embeddings(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_document: tuple[UUID, str],
) -> None:
    document_id, _ = seeded_document
    store = _store(session_factory)
    record = _record(document_id, "aspirin dosage guidance", _unit_vector(0))
    await store.upsert_chunks([record])

    async with session_factory() as session:
        await session.execute(text("DELETE FROM chunks WHERE id = :id"), {"id": record.chunk_id})
        await session.commit()

    assert await _count(session_factory, "chunk_embeddings") == 0
