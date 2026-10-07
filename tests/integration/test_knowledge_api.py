"""JWT -> real DI -> pgvector/FTS/RRF -> grounded answer -> existing trace tables.

Local model inference and generation are deterministic doubles in this suite.
The separately enabled real-model smoke test exercises the actual BGE weights.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.application.ports.retrieval import ChunkRecord
from app.core.config import get_settings
from app.core.container import Container, get_container
from app.domain.auth.value_objects import Role
from app.infrastructure.persistence.sql import tables
from app.presentation.api.app import create_app
from tests.integration.conftest import auth, login
from tests.integration.test_retrieval_store import migrated_database as migrated_database
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM, StubReranker


@dataclass
class KnowledgeAPI:
    client: TestClient
    container: Container
    engine: sa.Engine
    tokens: dict[Role, str]
    actor_id: UUID
    reranker: StubReranker
    llm: StubLLM

    def index(self, text: str = "The synthetic clinic opens on Monday.") -> UUID:
        document_id, chunk_id = uuid4(), uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                sa.insert(tables.documents).values(
                    id=document_id,
                    user_id=self.actor_id,
                    filename="synthetic-guide.pdf",
                    content=text,
                    status="COMPLETED",
                    metadata={},
                )
            )
        store = self.container.retrieval_store
        assert store is not None
        assert self.client.portal is not None
        self.client.portal.call(
            store.upsert_chunks,
            [
                ChunkRecord(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    text=text,
                    embedding=[1.0] + [0.0] * 383,
                    embedding_model="all-MiniLM-L6-v2",
                    embedding_dim=384,
                    embedding_version="1",
                    section="Opening hours",
                    page=3,
                    token_count=10,
                    document_version=1,
                    ingested_at=datetime.now(UTC),
                )
            ],
        )
        return chunk_id


@pytest.fixture
def knowledge_api(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[KnowledgeAPI]:
    password = secrets.token_urlsafe(24)
    for name, value in {
        "DATABASE__URL": migrated_database,
        "ENVIRONMENT": "development",
        "AUTH__SECRET_KEY": secrets.token_urlsafe(48),
        "AUTH__DEMO_PASSWORD": password,
        "AUTH__SEED_DEMO_ACCOUNTS": "true",
        "AUTH__BCRYPT_ROUNDS": "10",
        "LLM__PROVIDER": "ollama",
        "LLM__FALLBACK": "",
        "RETRIEVAL__RRF_K": "60",
        "RETRIEVAL__SCORE_THRESHOLD": "0.5",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_container.cache_clear()
    container = get_container()
    reranker, llm = StubReranker(), StubLLM()
    monkeypatch.setattr(container, "_embedding_provider", StubEmbeddings())
    monkeypatch.setattr(container, "_reranker", reranker)
    monkeypatch.setattr(container, "_llm_provider", llm)
    engine = sa.create_engine(
        sa.engine.make_url(migrated_database).set(drivername="postgresql+psycopg")
    )
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE documents, traces, users CASCADE"))
    try:
        with TestClient(create_app()) as client:
            tokens = {role: login(client, role, password) for role in Role}
            actor_id = UUID(
                client.get("/api/v1/auth/me", headers=auth(tokens[Role.ANALYST])).json()["id"]
            )
            yield KnowledgeAPI(client, container, engine, tokens, actor_id, reranker, llm)
    finally:
        engine.dispose()
        get_container.cache_clear()
        get_settings.cache_clear()


def test_real_retrieval_and_ask_citations_resolve_exactly_to_indexed_rows(
    knowledge_api: KnowledgeAPI,
) -> None:
    api = knowledge_api
    chunk_id = api.index()
    response = api.client.post(
        "/api/v1/ask", json={"question": "synthetic clinic"}, headers=auth(api.tokens[Role.ANALYST])
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["refused"] is False
    citation = data["citations"][0]
    assert set(citation) == {
        "document_id",
        "document_name",
        "section",
        "page",
        "chunk_id",
        "relevance_score",
        "text_snippet",
    }
    with api.engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT c.document_id, d.filename, c.section, c.page, c.text FROM chunks c "
                "JOIN documents d ON d.id=c.document_id WHERE c.id=:id"
            ),
            {"id": chunk_id},
        ).one()
    assert citation == {
        "document_id": str(row.document_id),
        "document_name": row.filename,
        "section": row.section,
        "page": row.page,
        "chunk_id": str(chunk_id),
        "relevance_score": 0.9,
        "text_snippet": row.text,
    }


def test_trace_uses_existing_tables_and_existing_ownership_controls(
    knowledge_api: KnowledgeAPI,
) -> None:
    api = knowledge_api
    chunk_id = api.index()
    response = api.client.post(
        "/api/v1/ask", json={"question": "synthetic clinic"}, headers=auth(api.tokens[Role.ANALYST])
    )
    assert response.status_code == 200, response.text
    trace_id = response.json()["trace_id"]
    with api.engine.connect() as connection:
        spans = connection.execute(
            sa.text("SELECT name, inputs, outputs, duration FROM spans WHERE trace_id=:id"),
            {"id": UUID(trace_id)},
        ).all()
    by_name = {span.name: span for span in spans}
    assert set(by_name) == {"retrieval.hybrid", "qa.ask"}
    retrieval = by_name["retrieval.hybrid"]
    assert retrieval.inputs == {"query": "synthetic clinic"}
    assert (
        retrieval.outputs["dense_count"],
        retrieval.outputs["keyword_count"],
        retrieval.outputs["fused_count"],
    ) == (1, 1, 1)
    assert retrieval.outputs["rrf_k"] == 60
    assert retrieval.outputs["selected_chunk_ids"] == [str(chunk_id)]
    assert retrieval.outputs["reranked"][0]["rrf_score"] == pytest.approx(2 / 61)
    assert retrieval.duration >= 0 and by_name["qa.ask"].outputs["refused"] is False
    assert (
        api.client.get(
            f"/api/v1/traces/{trace_id}", headers=auth(api.tokens[Role.ANALYST])
        ).status_code
        == 200
    )
    assert (
        api.client.get(
            f"/api/v1/traces/{trace_id}", headers=auth(api.tokens[Role.REVIEWER])
        ).status_code
        == 403
    )
    assert (
        api.client.get(
            f"/api/v1/traces/{trace_id}", headers=auth(api.tokens[Role.ADMIN])
        ).status_code
        == 200
    )


@pytest.mark.parametrize("endpoint,field", [("ask", "question"), ("retrieve", "query")])
def test_knowledge_endpoints_require_authentication(
    knowledge_api: KnowledgeAPI, endpoint: str, field: str
) -> None:
    response = knowledge_api.client.post(f"/api/v1/{endpoint}", json={field: "clinic"})
    assert response.status_code == 401


@pytest.mark.parametrize("query", ["", " ", "a" * 2001, "bad\x00query"])
def test_http_query_limits_are_enforced(knowledge_api: KnowledgeAPI, query: str) -> None:
    response = knowledge_api.client.post(
        "/api/v1/ask", json={"question": query}, headers=auth(knowledge_api.tokens[Role.ANALYST])
    )
    assert response.status_code == 422


def test_empty_corpus_returns_the_exact_successful_refusal(knowledge_api: KnowledgeAPI) -> None:
    response = knowledge_api.client.post(
        "/api/v1/ask", json={"question": "clinic"}, headers=auth(knowledge_api.tokens[Role.ANALYST])
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "Not enough information in the corpus"
    assert response.json()["citations"] == [] and response.json()["refused"] is True


def test_reranker_failure_is_safe_503_without_fallback(knowledge_api: KnowledgeAPI) -> None:
    api = knowledge_api
    api.index()
    api.reranker.override = []
    response = api.client.post(
        "/api/v1/retrieve", json={"query": "clinic"}, headers=auth(api.tokens[Role.ANALYST])
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Knowledge service is unavailable",
        "code": "KNOWLEDGE_UNAVAILABLE",
    }


def test_postgres_score_ties_are_stable_by_chunk_id(knowledge_api: KnowledgeAPI) -> None:
    api = knowledge_api
    ids = [api.index(), api.index()]
    store = api.container.retrieval_store
    assert store is not None
    for _ in range(2):
        assert api.client.portal is not None
        dense = api.client.portal.call(lambda: store.dense_search([1.0] + [0.0] * 383, top_k=2))
        keyword = api.client.portal.call(lambda: store.keyword_search("synthetic clinic", top_k=2))
        assert [item.chunk_id for item in dense] == sorted(ids)
        assert [item.chunk_id for item in keyword] == sorted(ids)
