"""Opt-in CPU smoke test with real MiniLM, BGE, PostgreSQL FTS and pgvector.

RUN_LOCAL_MODEL_TESTS=1 enables public weight loading. Normal CI runs the real
database/API tests plus the reranker contract tests without a multi-GB download.
The existing chat provider boundary is doubled; this test proves real retrieval,
reranking, source resolution and deterministic refusal before generation.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa

from app.application.ports.retrieval import ChunkRecord
from app.domain.auth.value_objects import Role
from app.infrastructure.embeddings.local_adapter import LocalEmbeddingAdapter
from app.infrastructure.reranking.local_adapter import (
    MODEL_NAME,
    MODEL_REVISION,
    LocalCrossEncoderReranker,
)
from tests.integration.conftest import auth
from tests.integration.test_knowledge_api import KnowledgeAPI
from tests.integration.test_knowledge_api import knowledge_api as knowledge_api
from tests.integration.test_retrieval_store import migrated_database as migrated_database

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_MODEL_TESTS") != "1",
    reason="Set RUN_LOCAL_MODEL_TESTS=1 to run real local model inference",
)


def test_real_dense_keyword_rrf_bge_citations_and_refusal(
    knowledge_api: KnowledgeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = knowledge_api
    texts = [
        "The synthetic clinic opening hours are Monday through Friday, 09:00 to 17:00.",
        "The synthetic clinic accepts appointment bookings through its online portal.",
        "The fictional archive stores old calendars and historical weather records.",
    ]
    ids = [api.index(text) for text in texts]
    embeddings = LocalEmbeddingAdapter()
    reranker = LocalCrossEncoderReranker(
        cache_directory=os.environ.get("RERANKER__CACHE_DIRECTORY")
    )
    monkeypatch.setattr(api.container, "_embedding_provider", embeddings)
    monkeypatch.setattr(api.container, "_reranker", reranker)
    assert api.client.portal is not None
    store = api.container.retrieval_store
    assert store is not None
    try:
        encoded = api.client.portal.call(embeddings.generate_embeddings, texts)
        records = []
        with api.engine.connect() as connection:
            for identifier, vector in zip(ids, encoded.vectors, strict=True):
                row = connection.execute(
                    sa.text("SELECT document_id,text,section,page FROM chunks WHERE id=:id"),
                    {"id": identifier},
                ).one()
                records.append(
                    ChunkRecord(
                        chunk_id=identifier,
                        document_id=row.document_id,
                        text=row.text,
                        embedding=vector,
                        embedding_model=encoded.model_name,
                        embedding_dim=encoded.dimensions,
                        embedding_version="1",
                        section=row.section,
                        page=row.page,
                        token_count=None,
                    )
                )
        api.client.portal.call(store.upsert_chunks, records)
        response = api.client.post(
            "/api/v1/retrieve",
            json={"query": "synthetic clinic opening hours"},
            headers=auth(api.tokens[Role.ANALYST]),
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["citations"] and data["citations"][0]["chunk_id"] == str(ids[0])
        assert data["citations"][0]["relevance_score"] >= 0.5
        for citation in data["citations"]:
            identifier = UUID(citation["chunk_id"])
            assert identifier in ids
            assert citation["text_snippet"] == texts[ids.index(identifier)]
            assert citation["section"] == "Opening hours" and citation["page"] == 3
        with api.engine.connect() as connection:
            trace: dict[str, Any] = connection.execute(
                sa.text("SELECT outputs FROM spans WHERE trace_id=:id AND name='retrieval.hybrid'"),
                {"id": UUID(data["trace_id"])},
            ).scalar_one()
        assert trace["dense_count"] == 3 and trace["keyword_count"] == 1
        assert trace["fused_count"] == 3 and trace["rrf_k"] == 60
        assert trace["reranker_model"] == MODEL_NAME == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert reranker._model is not None
        assert trace["reranked"][0]["rrf_score"] == pytest.approx(2 / 61)
        refusal = api.client.post(
            "/api/v1/ask",
            json={"question": "What is the orbital mass of fictional planet QX-999?"},
            headers=auth(api.tokens[Role.ANALYST]),
        )
        assert refusal.status_code == 200, refusal.text
        assert refusal.json()["answer"] == "Not enough information in the corpus"
        assert refusal.json()["refused"] is True and refusal.json()["citations"] == []
        assert api.llm.calls == []
        print(
            json.dumps(
                {
                    "model": MODEL_NAME,
                    "revision": MODEL_REVISION,
                    "rrf_k": trace["rrf_k"],
                    "dense_count": trace["dense_count"],
                    "keyword_count": trace["keyword_count"],
                    "fused_count": trace["fused_count"],
                    "reranked": trace["reranked"],
                    "citations_resolved": True,
                    "insufficient_evidence_refused": True,
                },
                sort_keys=True,
            )
        )
    finally:
        embeddings.close()
