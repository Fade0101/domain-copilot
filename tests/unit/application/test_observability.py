"""Correlation isolation, physical-call accounting and honest dependency status."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.application.auth.context import ExecutionContext
from app.application.errors import ProviderUnavailableError
from app.application.observability.context import (
    correlation_scope,
    current_trace,
    get_current_correlation_id,
    trace_scope,
)
from app.application.observability.health_service import HealthService
from app.application.ports.llm import CompletionRequest, CompletionResponse, StreamChunk
from app.application.retrieval.observability import RetrievalObserver
from app.core.config import ObservabilitySettings, Settings
from app.domain.observability.pricing import CostEstimator, ModelRate
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.observed import ObservedLLMProvider
from app.presentation.api.app import create_app
from app.presentation.api.dependencies import get_health_service
from tests.support.fakes import FixedClock, RecordingAuditSink
from tests.support.knowledge_fakes import principal

NOW = datetime(2026, 10, 1, tzinfo=UTC)
USAGE = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}


class Provider:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return CompletionResponse("private generated text", usage=USAGE, model="actual-model")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        if self.failure:
            raise self.failure
        yield StreamChunk(delta="private generated text", usage=USAGE)
        yield StreamChunk(finish_reason="stop", usage=USAGE, model="actual-model")
        yield StreamChunk(usage=USAGE)


def observed(
    provider: Provider, audit: RecordingAuditSink, name: str = "test-provider"
) -> ObservedLLMProvider:
    return ObservedLLMProvider(
        provider, RetrievalObserver(audit, FixedClock(NOW)), provider=name, model="requested-model"
    )


async def test_correlation_is_inherited_nested_and_isolated_between_tasks() -> None:
    owner = uuid4()

    async def request(correlation: str) -> None:
        with correlation_scope(correlation):
            with trace_scope(uuid4(), owner) as parent:
                await asyncio.sleep(0)
                assert get_current_correlation_id() == correlation
                assert ExecutionContext.from_principal(principal()).correlation_id == correlation
                with trace_scope(uuid4(), owner) as child:
                    assert child.run_id == parent.run_id
                    assert child.correlation_id == correlation
                assert current_trace() == parent
            assert current_trace() is None

    await asyncio.gather(request(str(uuid4())), request(str(uuid4())))
    assert current_trace() is None and get_current_correlation_id() is None
    with pytest.raises(RuntimeError), correlation_scope("outer"):
        with trace_scope(uuid4(), owner):
            raise RuntimeError("private")
    assert current_trace() is None and get_current_correlation_id() is None


async def test_complete_records_one_actual_provider_call_without_prompt_or_output_text() -> None:
    audit, provider = RecordingAuditSink(), Provider()
    request = CompletionRequest([{"role": "user", "content": "private prompt"}])
    correlation = str(uuid4())
    with trace_scope(uuid4(), uuid4(), correlation_id=correlation):
        result = await observed(provider, audit).complete(request)
    assert result.content == "private generated text"
    assert provider.requests == [request]
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    payload = json.loads(entry.detail["telemetry"])
    assert payload["model"] == "actual-model" and payload["usage"] == USAGE
    assert entry.correlation_id == correlation and entry.outcome == "completed"
    assert "private" not in json.dumps(entry.detail)


async def test_fallback_accounts_for_each_physical_attempt_under_its_own_provider() -> None:
    audit = RecordingAuditSink()
    primary = Provider(ProviderUnavailableError("secret endpoint"))
    secondary = Provider()
    llm = FallbackLLMProvider(
        observed(primary, audit, "primary"), observed(secondary, audit, "fallback")
    )
    with trace_scope(uuid4(), uuid4()):
        await llm.complete(CompletionRequest([]))
    entries = audit.entries
    assert [json.loads(entry.detail["telemetry"])["provider"] for entry in entries] == [
        "primary",
        "fallback",
    ]
    assert [entry.outcome for entry in entries] == ["error", "completed"]
    assert "usage" not in json.loads(entries[0].detail["telemetry"])
    assert "secret endpoint" not in str(entries)
    assert len({entry.correlation_id for entry in entries}) == 1


async def test_stream_usage_is_recorded_once_after_exhaustion_not_once_per_chunk() -> None:
    audit = RecordingAuditSink()
    with trace_scope(uuid4(), uuid4()):
        stream = observed(Provider(), audit).stream(CompletionRequest([]))
        first = await anext(stream)
        assert first.delta and not audit.entries
        remaining = [chunk async for chunk in stream]
    assert len(remaining) == 2 and len(audit.entries) == 1
    data = json.loads(audit.entries[0].detail["telemetry"])
    assert data["usage"] == USAGE and data["stream_complete"] is True


async def test_abandoned_stream_retains_partial_usage_and_does_not_claim_completion() -> None:
    audit = RecordingAuditSink()
    with trace_scope(uuid4(), uuid4()):
        stream = observed(Provider(), audit).stream(CompletionRequest([]))
        await anext(stream)
        await stream.aclose()
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    data = json.loads(entry.detail["telemetry"])
    assert entry.outcome == "error" and not data.get("stream_complete")
    assert data["usage"] == USAGE


async def test_stream_cleanup_keeps_original_identity_after_its_calling_context_changes() -> None:
    audit = RecordingAuditSink()
    original_id, owner, job_id, correlation = uuid4(), uuid4(), uuid4(), str(uuid4())
    with trace_scope(original_id, owner, job_id=job_id, correlation_id=correlation):
        stream = observed(Provider(), audit).stream(CompletionRequest([]))
        await anext(stream)
    with trace_scope(uuid4(), uuid4(), job_id=uuid4()):
        await stream.aclose()
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.actor_id == str(owner) and entry.resource_id == str(original_id)
    assert entry.correlation_id == correlation and entry.detail["job_id"] == str(job_id)


async def test_groq_usage_only_final_chunk_is_not_discarded() -> None:
    async def chunks():
        yield SimpleNamespace(choices=[], x_groq={"usage": USAGE}, model="returned-model")

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=chunks())
    adapter = GroqAdapter(client=client)
    result = [chunk async for chunk in adapter.stream(CompletionRequest([]))]
    assert len(result) == 1 and result[0].usage == USAGE
    assert result[0].model == "returned-model"


def test_prices_require_explicit_provider_model_rate_and_usage() -> None:
    rate = ModelRate(2.0, 4.0, "synthetic test schedule v1")
    estimator = CostEstimator({("provider-a", "model"): rate})
    assert estimator.estimate("provider-a", "model", 1_000_000, 500_000) == 4.0
    assert estimator.estimate("provider-b", "model", 1_000_000, 500_000) is None
    assert estimator.estimate("provider-a", "unknown", 1, 1) is None
    assert estimator.estimate("provider-a", "model", 1, None) is None
    assert estimator.estimate("ollama", "model", 1, 1) is None
    assert not ObservabilitySettings().rates


def test_duplicate_or_unversioned_pricing_configuration_is_rejected() -> None:
    rate = {
        "provider": "test",
        "model": "model",
        "prompt_per_million": 1,
        "completion_per_million": 2,
        "source": "test-v1",
    }
    with pytest.raises(ValidationError):
        ObservabilitySettings.model_validate({"rates": [rate, rate]})
    with pytest.raises(ValidationError):
        ObservabilitySettings.model_validate({"rates": [{**rate, "source": " "}]})
    with pytest.raises(ValidationError):
        ObservabilitySettings.model_validate(
            {"rates": [{**rate, "prompt_per_million": float("nan")}]}
        )


class Probes:
    def __init__(self, checks: dict[str, Callable[[], Awaitable[bool]]]) -> None:
        self.checks = checks

    def probes(self) -> dict[str, Callable[[], Awaitable[bool]]]:
        return self.checks


async def test_readiness_probes_are_concurrent_bounded_and_sanitized() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def healthy() -> bool:
        await entered.wait()
        return True

    async def failed() -> bool:
        raise RuntimeError("postgres://secret:password@private-host")

    async def blocked() -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
            return True
        finally:
            cancelled.set()

    service = HealthService(
        Probes({"postgres": healthy, "redis": failed, "llm": blocked}), timeout_seconds=0.1
    )
    ready, payload = await service.check_readiness()
    assert not ready and cancelled.is_set()
    assert payload["dependencies"] == {
        "postgres": {"status": "ok"},
        "redis": {"status": "unavailable"},
        "llm": {"status": "timeout"},
    }
    assert "secret" not in json.dumps(payload)


async def test_http_health_and_ready_use_real_probe_results_and_echo_safe_correlation() -> None:
    called = 0

    async def probe() -> bool:
        nonlocal called
        called += 1
        return False

    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    app.dependency_overrides[get_health_service] = lambda: HealthService(
        Probes({"postgres": probe})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        correlation = str(uuid4())
        live = await client.get("/health", headers={"X-Correlation-ID": correlation})
        assert live.status_code == 200 and called == 0
        assert live.headers["X-Correlation-ID"] == correlation
        ready = await client.get("/ready", headers={"X-Correlation-ID": "bad-id"})
        assert ready.status_code == 503 and called == 1
        UUID(ready.headers["X-Correlation-ID"])
        assert ready.headers["Cache-Control"] == "no-store"
        missing = await client.get("/missing")
        assert missing.status_code == 404
        UUID(missing.headers["X-Correlation-ID"])


async def test_unhandled_http_error_preserves_correlation_without_exposing_exception() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    @app.get("/failure")
    async def failure():
        raise RuntimeError("private-error")

    correlation = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/failure", headers={"X-Correlation-ID": correlation})
    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == correlation
    assert "private-error" not in response.text
