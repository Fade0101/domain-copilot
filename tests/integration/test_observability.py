"""Sync asks, trace queries and cost accounting against migrated PostgreSQL/pgvector."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.application.auth.context import Principal
from app.application.ports.llm import CompletionRequest, CompletionResponse
from app.domain.auth.value_objects import Role, UserId
from app.domain.observability.entities import CostEntry, Span, Trace, TraceQuery
from app.domain.observability.pricing import CostEstimator, ModelRate
from app.infrastructure.embeddings.observed import ObservedEmbeddingProvider
from app.infrastructure.llm.observed import ObservedLLMProvider
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import CostLedgerModel, UserModel
from app.infrastructure.persistence.sql.trace_store import PostgresTraceStore
from tests.integration.conftest import auth
from tests.integration.test_knowledge_api import KnowledgeAPI
from tests.integration.test_knowledge_api import knowledge_api as knowledge_api
from tests.integration.test_retrieval_store import migrated_database as migrated_database
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM

USAGE = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}


class MeteredLLM(StubLLM):
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        result = await super().complete(request)
        return replace(result, usage=USAGE, model="metered-model")


def instrument(api: KnowledgeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    observer = api.container._retrieval_observer
    monkeypatch.setattr(
        observer.sink,
        "_estimator",
        CostEstimator({("test-provider", "metered-model"): ModelRate(2, 4, "test schedule v1")}),
    )
    monkeypatch.setattr(
        api.container,
        "_llm_provider",
        ObservedLLMProvider(MeteredLLM(), observer, provider="test-provider", model="requested"),
    )
    monkeypatch.setattr(
        api.container,
        "_embedding_provider",
        ObservedEmbeddingProvider(
            StubEmbeddings(), observer, provider="sentence_transformers", model="all-MiniLM-L6-v2"
        ),
    )


def ask(api: KnowledgeAPI, correlation: str | None = None):
    response = api.client.post(
        "/api/v1/ask",
        json={"question": "When does the synthetic clinic open?"},
        headers={
            **auth(api.tokens[Role.ANALYST]),
            **({"X-Correlation-ID": correlation} if correlation else {}),
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_sync_ask_has_durable_retrieval_llm_and_request_spans_and_honest_usage(
    knowledge_api: KnowledgeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = knowledge_api
    instrument(api, monkeypatch)
    api.index()
    correlation = str(uuid4())
    response = ask(api, correlation)
    assert not response.json()["refused"]
    trace_id = response.json()["trace_id"]
    assert response.headers["X-Correlation-ID"] == correlation
    headers = auth(api.tokens[Role.ANALYST])
    detail = api.client.get(f"/api/v1/traces/{trace_id}/spans", headers=headers)
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["trace"]["run_id"] == trace_id
    assert data["trace"]["correlation_id"] == correlation
    assert data["trace"]["job_id"] is None and data["trace"]["end_time"] is not None
    assert {span["step_type"] for span in data["spans"]} == {
        "request",
        "retrieval",
        "llm",
        "embedding",
    }
    assert all(span["start_time"] and span["end_time"] for span in data["spans"])
    assert all(span["duration"] >= 0 for span in data["spans"])
    usage = api.client.get("/api/v1/usage", params={"run_id": trace_id}, headers=headers)
    assert usage.status_code == 200, usage.text
    report = usage.json()
    # Retrieval itself and the enclosing ask cannot charge the LLM usage a second time.
    assert report["calls"] == 2 and report["total_tokens"] == 12
    assert report["known_estimated_cost"] == pytest.approx(0.000032)
    assert report["estimated_cost"] is None and report["cost_complete"] is False
    assert report["is_estimate"] is True and report["currency"] == "USD"
    llm = next(item for item in report["breakdown"] if item["provider"] == "test-provider")
    assert llm["model"] == "metered-model" and llm["estimated_cost"] == pytest.approx(0.000032)
    embedding = next(
        item for item in report["breakdown"] if item["provider"] == "sentence_transformers"
    )
    assert embedding["unavailable_usage_calls"] == 1
    assert embedding["estimated_cost"] is None
    with api.engine.connect() as connection:
        rows = connection.execute(sa.select(CostLedgerModel)).mappings().all()
        assert len(rows) == 2
        row = next(item for item in rows if item["provider"] == "test-provider")
        assert row["rate_source"] == "test schedule v1" and row["cost_status"] == "estimated"
        assert row["tokens_prompt"] == 8 and row["tokens_completion"] == 4
    # Querying later never recalculates the ledger using a different deployment's prices.
    monkeypatch.setattr(api.container._retrieval_observer.sink, "_estimator", CostEstimator())
    assert (
        api.client.get("/api/v1/usage", params={"run_id": trace_id}, headers=headers).json()
        == report
    )


def test_trace_filters_pagination_owner_isolation_and_fresh_roles(
    knowledge_api: KnowledgeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = knowledge_api
    instrument(api, monkeypatch)
    api.index()
    first = ask(api)
    second = ask(api)
    first_id = first.json()["trace_id"]
    headers = auth(api.tokens[Role.ANALYST])
    for params in (
        {"run_id": first_id},
        {"correlation_id": first.headers["X-Correlation-ID"]},
        {"user_id": str(api.actor_id), "run_id": first_id},
    ):
        page = api.client.get("/api/v1/traces", params=params, headers=headers)
        assert page.status_code == 200 and [item["id"] for item in page.json()["items"]] == [
            first_id
        ]
    page = api.client.get("/api/v1/traces", params={"limit": 1}, headers=headers).json()
    assert [item["id"] for item in page["items"]] == [second.json()["trace_id"]]
    older = api.client.get(
        "/api/v1/traces", params={"limit": 1, "offset": 1}, headers=headers
    ).json()
    assert older["items"][0]["id"] == first_id
    start = older["items"][0]["start_time"]
    end = page["items"][0]["start_time"]
    between = api.client.get(
        "/api/v1/traces", params={"start_time": start, "end_time": end}, headers=headers
    )
    assert [item["id"] for item in between.json()["items"]] == [first_id]
    for path in ("/api/v1/traces", "/api/v1/usage", f"/api/v1/traces/{first_id}/spans"):
        assert api.client.get(path).status_code == 401
    assert (
        api.client.get(
            f"/api/v1/traces/{first_id}/spans", headers=auth(api.tokens[Role.REVIEWER])
        ).status_code
        == 403
    )
    assert (
        api.client.get(
            "/api/v1/traces",
            params={"user_id": str(api.actor_id)},
            headers=auth(api.tokens[Role.REVIEWER]),
        ).status_code
        == 403
    )
    assert not api.client.get(
        "/api/v1/traces",
        params={"correlation_id": first.headers["X-Correlation-ID"]},
        headers=auth(api.tokens[Role.REVIEWER]),
    ).json()["items"]
    assert (
        api.client.get(
            f"/api/v1/traces/{first_id}/spans", headers=auth(api.tokens[Role.ADMIN])
        ).status_code
        == 200
    )
    admin_id = api.client.get("/api/v1/auth/me", headers=auth(api.tokens[Role.ADMIN])).json()["id"]
    # A token minted while admin cannot retain cross-owner access after demotion.
    with api.engine.begin() as connection:
        connection.execute(
            sa.update(UserModel).where(UserModel.id == UUID(admin_id)).values(role="analyst")
        )
    assert (
        api.client.get(
            f"/api/v1/traces/{first_id}/spans", headers=auth(api.tokens[Role.ADMIN])
        ).status_code
        == 403
    )
    for params in ({"start_time": "2026-01-01T00:00:00"}, {"start_time": end, "end_time": start}):
        assert api.client.get("/api/v1/traces", params=params, headers=headers).status_code == 422


def test_new_store_replays_persisted_traces_without_redis_or_in_memory_state(
    knowledge_api: KnowledgeAPI, migrated_database: str
) -> None:
    api = knowledge_api
    api.index()
    trace_id = UUID(ask(api).json()["trace_id"])

    async def reopen():
        database = Database(migrated_database, pooling=False)
        try:
            store = PostgresTraceStore(database.session_factory)
            trace = await store.get_trace(trace_id)
            assert trace and trace.user_id == api.actor_id
            assert len(await store.get_spans(trace_id, limit=100, offset=0)) == 2
            assert len(await store.query_traces(TraceQuery(run_id=trace_id))) == 1
        finally:
            await database.dispose()

    assert api.client.portal is not None
    api.client.portal.call(reopen)


def test_concurrent_record_retries_cannot_double_count_one_provider_span(
    knowledge_api: KnowledgeAPI,
) -> None:
    api = knowledge_api
    now = datetime.now(UTC)
    identifier, span_id = uuid4(), uuid4()
    trace = Trace(identifier, api.actor_id, now, run_id=identifier)
    span = Span(span_id, identifier, "llm.complete", "llm", {}, {}, start_time=now, end_time=now)
    cost = CostEntry(
        id=span_id,
        span_id=span_id,
        trace_id=identifier,
        provider="test",
        model="model",
        tokens_prompt=5,
        tokens_completion=3,
        total_tokens=8,
        usage_status="provider_reported",
        cost=0.01,
        cost_status="estimated",
        created_at=now,
        rate_source="test-v1",
    )

    async def write():
        assert api.container.database is not None
        store = PostgresTraceStore(api.container.database.session_factory)
        await asyncio.gather(*(store.record(trace, span, cost) for _ in range(6)))
        assert len(await store.get_spans(identifier, limit=100, offset=0)) == 1
        usage = await store.usage(TraceQuery(run_id=identifier, limit=1, offset=100))
        assert len(usage) == 1 and usage[0].calls == 1
        assert usage[0].estimated_cost == 0.01

    assert api.client.portal is not None
    api.client.portal.call(write)
    with api.engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            sa.insert(CostLedgerModel).values(
                id=uuid4(),
                trace_id=identifier,
                span_id=span_id,
                tokens_prompt=5,
                tokens_completion=3,
                cost=0.01,
            )
        )


def test_trace_service_reloads_identity_even_when_given_an_old_principal(
    knowledge_api: KnowledgeAPI,
) -> None:
    api = knowledge_api

    async def check():
        user = await api.container.user_repository.get_by_id(UserId(str(api.actor_id)))
        assert user is not None
        actor = Principal.from_user(user)
        forged = replace(actor, role=Role.ADMIN)
        scoped = await api.container.trace_service().scoped_query(forged, TraceQuery())
        assert scoped.user_id == api.actor_id

    assert api.client.portal is not None
    api.client.portal.call(check)
