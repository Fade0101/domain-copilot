"""Unit tests for the retrieval port (BRD AC-2.1).

Exercises :class:`IRetrievalStore` through a pure-Python fake, proving the
contract is usable with no database, no SQLAlchemy and no pgvector present. The
PostgreSQL ranking semantics are covered separately by
``tests/integration/test_retrieval_store.py`` against real pgvector.

Also asserts the port module itself stays SDK-free, which is the property that
lets the store be swapped without touching application code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID, uuid4

from app.application.ports.retrieval import ChunkRecord, IRetrievalStore, SearchHit
from tests.support.fakes import FakeRetrievalStore

_PORT_SOURCE = (
    Path(__file__).resolve().parents[3] / "app" / "application" / "ports" / "retrieval.py"
)

# Everything the port is allowed to import. Anything else -- in particular a
# database or vector SDK -- would make the contract non-portable.
_ALLOWED_IMPORTS = {"__future__", "dataclasses", "datetime", "typing", "uuid"}

_DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
_MODEL = "all-MiniLM-L6-v2"
_VERSION = "1"


def _record(text: str, embedding: list[float], **overrides: object) -> ChunkRecord:
    defaults: dict[str, object] = {
        "chunk_id": uuid4(),
        "document_id": _DOCUMENT_ID,
        "text": text,
        "embedding": embedding,
        "embedding_model": _MODEL,
        "embedding_dim": len(embedding),
        "embedding_version": _VERSION,
        "section": "Dosage",
        "page": 7,
        "token_count": 12,
    }
    defaults.update(overrides)
    return ChunkRecord(**defaults)  # type: ignore[arg-type]


def _store() -> FakeRetrievalStore:
    return FakeRetrievalStore(document_names={_DOCUMENT_ID: "guideline.pdf"})


def test_port_module_imports_only_stdlib() -> None:
    tree = ast.parse(_PORT_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= _ALLOWED_IMPORTS, f"port imports non-stdlib modules: {imported}"


def test_fake_satisfies_the_port() -> None:
    assert isinstance(_store(), IRetrievalStore)


async def test_dense_search_ranks_by_similarity_and_reports_provenance() -> None:
    store = _store()
    near = _record("aspirin dosage guidance", [1.0, 0.0, 0.0])
    far = _record("unrelated filing instructions", [0.0, 1.0, 0.0])
    await store.upsert_chunks([far, near])

    hits = await store.dense_search([1.0, 0.0, 0.0], top_k=2)

    assert [hit.chunk_id for hit in hits] == [near.chunk_id, far.chunk_id]
    assert hits[0].score > hits[1].score
    assert hits[0].embedding_model == _MODEL
    assert hits[0].embedding_dim == 3
    assert hits[0].embedding_version == _VERSION


async def test_keyword_search_omits_embedding_provenance() -> None:
    store = _store()
    record = _record("aspirin dosage guidance", [1.0, 0.0, 0.0])
    await store.upsert_chunks([record])

    hits = await store.keyword_search("aspirin", top_k=5)

    assert [hit.chunk_id for hit in hits] == [record.chunk_id]
    hit = hits[0]
    assert hit.embedding_model is None
    assert hit.embedding_dim is None
    assert hit.embedding_version is None


async def test_keyword_search_returns_only_matching_chunks() -> None:
    store = _store()
    match = _record("aspirin dosage guidance", [1.0, 0.0, 0.0])
    other = _record("warfarin interactions", [0.0, 1.0, 0.0])
    await store.upsert_chunks([match, other])

    hits = await store.keyword_search("aspirin", top_k=5)

    assert [hit.chunk_id for hit in hits] == [match.chunk_id]


async def test_citation_metadata_survives_the_round_trip() -> None:
    store = _store()
    record = _record("aspirin dosage guidance", [1.0, 0.0, 0.0], section="Interactions", page=3)
    await store.upsert_chunks([record])

    for hit in (
        (await store.dense_search([1.0, 0.0, 0.0], top_k=1))[0],
        (await store.keyword_search("aspirin", top_k=1))[0],
    ):
        assert hit.document_id == _DOCUMENT_ID
        assert hit.document_name == "guideline.pdf"
        assert hit.section == "Interactions"
        assert hit.page == 3
        assert hit.snippet == "aspirin dosage guidance"


async def test_dense_and_keyword_search_are_independently_callable() -> None:
    store = _store()
    await store.upsert_chunks([_record("aspirin dosage guidance", [1.0, 0.0, 0.0])])

    # Neither call primes or depends on the other: a caller may use one alone.
    assert await store.keyword_search("aspirin", top_k=1)
    assert await store.dense_search([1.0, 0.0, 0.0], top_k=1)


async def test_repeated_upsert_is_idempotent() -> None:
    store = _store()
    record = _record("aspirin dosage guidance", [1.0, 0.0, 0.0])

    await store.upsert_chunks([record])
    await store.upsert_chunks([record])

    assert len(await store.dense_search([1.0, 0.0, 0.0], top_k=10)) == 1


def test_search_hit_is_immutable() -> None:
    hit = SearchHit(
        document_id=_DOCUMENT_ID,
        chunk_id=uuid4(),
        document_name="guideline.pdf",
        section=None,
        page=None,
        snippet="text",
        score=1.0,
        embedding_model=None,
        embedding_dim=None,
        embedding_version=None,
    )
    try:
        hit.score = 2.0  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("SearchHit must be immutable")
