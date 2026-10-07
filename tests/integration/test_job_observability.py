"""Durable job/agent/tool traces, worker correlation and per-job provider accounting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.application.agents.contracts import CaseSummary
from app.application.auth.context import Principal
from app.application.errors import JobPaused
from app.application.jobs.generation import GenerationJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.observability.health_service import HealthService
from app.application.ports.jobs import IJobContext
from app.application.ports.llm import CompletionRequest, CompletionResponse, StreamChunk, ToolCall
from app.domain.auth.value_objects import Role, UserId
from app.domain.jobs.entities import JobState
from app.domain.observability.entities import TraceQuery
from app.domain.observability.pricing import CostEstimator, ModelRate
from app.domain.workflow.entities import WorkflowRun
from app.infrastructure.llm.observed import ObservedLLMProvider
from app.infrastructure.observability.health import ChatProbe, DependencyHealthChecks
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import CostLedgerModel
from app.infrastructure.system.clock import SystemClock
from app.presentation.api.dependencies import get_health_service
from tests.integration.test_job_queue import Environment, wait_for, worker
from tests.integration.test_job_queue import database_url as database_url
from tests.integration.test_job_queue import jobs as jobs
from tests.integration.test_job_streaming import StreamAPI
from tests.integration.test_job_streaming import api as api
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM, StubReranker

USAGE = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}


class PricedProvider:
    calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse("text", usage=USAGE, model="test-model")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk(delta="text", usage=USAGE, model="test-model")
        yield StreamChunk(finish_reason="stop", usage=USAGE, model="test-model")
        yield StreamChunk(usage=USAGE, model="test-model")


@pytest.mark.parametrize("streaming", [False, True])
async def test_job_usage_is_durable_and_counted_once_for_complete_and_stream(
    api: StreamAPI, jobs: Environment, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    observer = api.container._retrieval_observer
    monkeypatch.setattr(
        observer.sink,
        "_estimator",
        CostEstimator({("test-provider", "test-model"): ModelRate(2, 4, "synthetic v1")}),
    )
    provider = PricedProvider()
    handler = GenerationJobHandler(
        lambda: ObservedLLMProvider(
            provider, observer, provider="test-provider", model="test-model"
        )
    )
    runner = JobRunner(
        jobs.runtime.service.store, JobHandlerRegistry([handler]), SystemClock(), observer=observer
    )
    correlation = uuid4()
    job = await jobs.runtime.service.submit(
        "llm.generate",
        {"prompt": "synthetic", "stream": streaming},
        user_id=jobs.owner,
        correlation_id=correlation,
    )
    await runner.run(job.id)
    await runner.run(job.id)
    assert provider.calls == 1
    assert (await jobs.runtime.service.get(job.id)).state == JobState.COMPLETED
    response = await api.client.get(
        "/api/v1/usage", params={"job_id": str(job.id)}, headers=api.auth()
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["calls"] == 1 and report["total_tokens"] == 12
    assert report["estimated_cost"] == pytest.approx(0.000032)
    assert report["is_estimate"] and report["usage_complete"] and report["cost_complete"]
    traces = await api.container.trace_service().store.query_traces(TraceQuery(job_id=job.id))
    assert len(traces) == 1 and traces[0].correlation_id == str(correlation)
    assert traces[0].run_id == correlation
    spans = await api.container.trace_service().store.get_spans(traces[0].id, limit=100, offset=0)
    assert {span.step_type for span in spans} == {"llm", "job"}
    assert next(span for span in spans if span.step_type == "job").outputs["state"] == "COMPLETED"
    with jobs.runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(CostLedgerModel)) == 1


@pytest.mark.parametrize(
    "outcome,state", [("failure", "FAILED"), ("pause", "STARTED"), ("cancel", "CANCELLED")]
)
async def test_job_span_records_real_failure_pause_and_cancellation(
    api: StreamAPI, jobs: Environment, outcome: str, state: str
) -> None:
    class Handler:
        operation_type = "diagnostic"

        def validate(self, payload: dict[str, Any]) -> None:
            pass

        async def run(self, context: IJobContext) -> dict[str, Any]:
            if outcome == "pause":
                raise JobPaused()
            if outcome == "failure":
                raise RuntimeError("private handler input")
            await jobs.runtime.service.store.request_cancel(context.job_id, datetime.now(UTC))
            await context.check_cancelled()
            return {}

    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    runner = JobRunner(
        jobs.runtime.service.store,
        JobHandlerRegistry([Handler()]),
        SystemClock(),
        observer=api.container._retrieval_observer,
    )
    await runner.run(job.id)
    store = api.container.trace_service().store
    trace = (await store.query_traces(TraceQuery(job_id=job.id)))[0]
    spans = await store.get_spans(trace.id, limit=100, offset=0)
    assert len(spans) == 1 and spans[0].outputs["state"] == state
    assert (
        spans[0].outputs["outcome"]
        == {"pause": "paused", "failure": "error", "cancel": "cancelled"}[outcome]
    )
    assert "private handler input" not in json.dumps(spans[0].outputs)
    assert trace.end_time is not None


async def test_http_correlation_survives_real_celery_process_and_trace_store_reopen(
    api: StreamAPI, jobs: Environment, tmp_path: Path
) -> None:
    correlation = str(uuid4())
    with worker(jobs, tmp_path):
        response = await api.client.post(
            "/api/v1/jobs",
            json={"operation_type": "diagnostic", "payload": {}},
            headers={**api.auth(Role.ADMIN), "X-Correlation-ID": correlation},
        )
        assert response.status_code == 202, response.text
        job_id = UUID(response.json()["job_id"])
        job = await wait_for(jobs, job_id, JobState.COMPLETED)
        assert str(job.correlation_id) == correlation
        store = api.container.trace_service().store
        async with asyncio.timeout(10):
            while True:
                traces = await store.query_traces(TraceQuery(job_id=job_id))
                if traces and traces[0].end_time:
                    break
                await asyncio.sleep(0.05)
    # The writer has exited; this query uses API/database state, not its process memory.
    page = await api.client.get(
        "/api/v1/traces", params={"correlation_id": correlation}, headers=api.auth(Role.ADMIN)
    )
    assert page.status_code == 200, page.text
    trace = page.json()["items"][0]
    assert trace["job_id"] == str(job_id) and trace["correlation_id"] == correlation
    spans = await api.client.get(
        f"/api/v1/traces/{trace['id']}/spans", headers=api.auth(Role.ADMIN)
    )
    assert spans.json()["spans"][0]["name"] == "job.execute"


async def test_all_agent_and_tool_spans_keep_job_run_owner_and_correlation(
    api: StreamAPI, jobs: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = api.container
    observer = container._retrieval_observer
    monkeypatch.setattr(container, "_embedding_provider", StubEmbeddings())
    monkeypatch.setattr(container, "_reranker", StubReranker())
    # An empty model response deliberately exercises agent refusal paths as well.
    monkeypatch.setattr(
        container,
        "_llm_provider",
        ObservedLLMProvider(
            StubLLM(content="No supported claims"), observer, provider="test", model="test-model"
        ),
    )
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    assert job.correlation_id
    workflow_id = job.correlation_id
    now = datetime.now(UTC)
    await container.workflow_repository.save(
        WorkflowRun(
            id=workflow_id,
            user_id=jobs.owner,
            correlation_id=str(job.correlation_id),
            case_summary="Synthetic case",
            created_at=now,
            updated_at=now,
            approval_job_id=job.id,
        )
    )

    class Handler:
        operation_type = "diagnostic"

        def validate(self, payload: dict[str, Any]) -> None:
            pass

        async def run(self, context: IJobContext) -> dict[str, Any]:
            user = await container.user_repository.get_by_id(UserId(str(jobs.owner)))
            assert user
            principal = Principal.from_user(user)
            tools = container.clinical_tool_factory().for_guideline_researcher(
                principal, workflow_id
            )
            result = await tools.execute(
                ToolCall("search-1", "search_corpus", '{"query":"synthetic clinic"}')
            )
            assert json.loads(result.output)["ok"]
            case = CaseSummary(workflow_id, "synthetic clinic", "Synthetic case")
            findings = await container.guideline_researcher_agent(principal, workflow_id).execute(
                case
            )
            verdict = await container.safety_checker_agent(principal, workflow_id).execute(findings)
            draft = await container.documentation_drafter_agent(principal, workflow_id).execute(
                verdict, case
            )
            assert draft.refused
            return {"refused": True}

    await JobRunner(
        jobs.runtime.service.store,
        JobHandlerRegistry([Handler()]),
        SystemClock(),
        observer=observer,
    ).run(job.id)
    assert (await jobs.runtime.service.get(job.id)).state == JobState.COMPLETED
    store = container.trace_service().store
    traces = await store.query_traces(TraceQuery(run_id=workflow_id))
    assert len(traces) >= 3
    assert {trace.correlation_id for trace in traces} == {str(job.correlation_id)}
    assert {trace.user_id for trace in traces} == {jobs.owner}
    assert {trace.job_id for trace in traces} == {job.id}
    spans = [
        span for trace in traces for span in await store.get_spans(trace.id, limit=100, offset=0)
    ]
    assert {"agent", "tool", "retrieval", "llm", "job"} <= {span.step_type for span in spans}
    assert {span.name for span in spans if span.step_type == "agent"} == {
        "agent.guideline_researcher",
        "agent.safety_checker",
        "agent.documentation_drafter",
    }


async def test_cancelled_stream_cannot_present_partial_usage_as_a_complete_cost(
    api: StreamAPI, jobs: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = api.container._retrieval_observer
    monkeypatch.setattr(
        observer.sink,
        "_estimator",
        CostEstimator({("test", "test-model"): ModelRate(2, 4, "synthetic v1")}),
    )
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "cancel while generating", "stream": True}, user_id=jobs.owner
    )

    class CancellingProvider(PricedProvider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(delta="text", usage=USAGE)
            await jobs.runtime.service.store.request_cancel(job.id, datetime.now(UTC))
            yield StreamChunk(finish_reason="stop", usage=USAGE)

    runner = JobRunner(
        jobs.runtime.service.store,
        JobHandlerRegistry(
            [
                GenerationJobHandler(
                    lambda: ObservedLLMProvider(
                        CancellingProvider(), observer, provider="test", model="test-model"
                    )
                )
            ]
        ),
        SystemClock(),
        observer=observer,
    )
    await runner.run(job.id)
    assert (await jobs.runtime.service.get(job.id)).state == JobState.CANCELLED
    response = await api.client.get(
        "/api/v1/usage", params={"job_id": str(job.id)}, headers=api.auth()
    )
    report = response.json()
    assert report["calls"] == 1 and report["total_tokens"] == 12
    assert not report["usage_complete"] and not report["cost_complete"]
    assert report["estimated_cost"] is None


@pytest.mark.parametrize("unavailable", [None, "postgres", "redis"])
async def test_readiness_checks_live_postgres_and_redis_and_liveness_stays_up(
    api: StreamAPI, jobs: Environment, monkeypatch: pytest.MonkeyPatch, unavailable: str | None
) -> None:
    async def provider_ready(self) -> bool:
        return True

    monkeypatch.setattr(ChatProbe, "check", provider_ready)
    assert api.container.database
    bad_database = Database("postgresql+psycopg://probe:probe@127.0.0.1:1/postgres", pooling=False)
    checks = DependencyHealthChecks(
        bad_database.session_factory
        if unavailable == "postgres"
        else api.container.database.session_factory,
        "redis://127.0.0.1:1" if unavailable == "redis" else jobs.broker,
        StubEmbeddings(),
        {"llm_primary": ChatProbe("ollama", "test")},
    )
    api.app.dependency_overrides[get_health_service] = lambda: HealthService(
        checks, timeout_seconds=1
    )
    try:
        response = await api.client.get("/ready")
        assert response.status_code == (503 if unavailable else 200), response.text
        dependencies = response.json()["dependencies"]
        if unavailable:
            assert dependencies[unavailable]["status"] != "ok"
        else:
            assert all(value["status"] == "ok" for value in dependencies.values())
        assert (await api.client.get("/health")).status_code == 200
    finally:
        await checks.close()
        await bad_database.dispose()
