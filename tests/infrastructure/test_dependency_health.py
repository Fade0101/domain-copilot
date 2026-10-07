"""Provider readiness uses non-generating endpoints and validates returned capabilities."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.application.errors import ObservabilityUnavailableError
from app.application.observability.health_service import HealthService
from app.application.ports.embeddings import EmbeddingResult
from app.infrastructure.observability.health import ChatProbe, DependencyHealthChecks
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.sql.trace_store import PostgresTraceStore


@pytest.mark.parametrize("provider", ["groq", "ollama"])
async def test_chat_probe_checks_model_access_without_spending_generation_tokens(
    monkeypatch, provider
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"id": "test-model", "active": True, "model_info": {"size": 1}}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert await ChatProbe(provider, "test-model", "http://provider.test", "fake-key").check()
    assert len(requests) == 1
    request = requests[0]
    assert "/chat" not in request.url.path and "/completions" not in request.url.path
    if provider == "groq":
        assert request.method == "GET" and request.url.path == "/openai/v1/models/test-model"
    else:
        assert request.url.path == "/api/show"
        assert json.loads(request.content) == {"model": "test-model"}


@pytest.mark.parametrize(
    "body", [{}, {"id": "wrong", "active": True}, {"id": "test-model", "active": False}]
)
async def test_groq_probe_does_not_report_missing_or_disabled_models_as_ready(monkeypatch, body):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)), **kw
        ),
    )
    assert not await ChatProbe("groq", "test-model", api_key="fake-key").check()


async def test_missing_credentials_are_not_reported_as_provider_readiness():
    assert not await ChatProbe("groq", "test-model").check()


async def test_unreachable_trace_database_returns_a_typed_unavailable_error():
    database = Database("postgresql://probe:probe@127.0.0.1:1/postgres", pooling=False)
    try:
        with pytest.raises(ObservabilityUnavailableError, match="Trace storage is unavailable"):
            await PostgresTraceStore(database.session_factory).get_trace(uuid4())
    finally:
        await database.dispose()


async def test_cold_embedding_readiness_reuses_one_inflight_check_and_closes_it():
    class Embeddings:
        calls = 0

        async def generate_embeddings(self, texts):
            self.calls += 1
            await asyncio.Event().wait()
            return EmbeddingResult([[1]], "model", 1)

    embeddings = Embeddings()
    checks = DependencyHealthChecks(None, "redis://127.0.0.1:1", embeddings, {})
    service = HealthService(checks, timeout_seconds=0.03)
    try:
        for _ in range(2):
            ready, report = await service.check_readiness()
            assert not ready and report["dependencies"]["embeddings"]["status"] == "timeout"
        assert embeddings.calls == 1
    finally:
        await checks.close()
