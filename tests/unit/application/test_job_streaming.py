"""Provider-neutral generation, durable publication and cooperative cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.errors import ProviderUnavailableError
from app.application.jobs.generation import GenerationJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.application.ports.llm import CompletionRequest, CompletionResponse, StreamChunk, ToolCall
from app.domain.jobs.entities import JobState
from app.domain.jobs.events import JobEventType
from app.domain.shared.errors import InvariantViolationError
from tests.support.fakes import FixedClock, SequentialIdGenerator
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

NOW = datetime(2026, 10, 6, tzinfo=UTC)
OWNER = UUID(int=100)


class ScriptedProvider:
    def __init__(self, items: list[StreamChunk | Exception] | None = None) -> None:
        self.items = (
            items
            if items is not None
            else [
                StreamChunk(delta="Hello"),
                StreamChunk(delta=" world", finish_reason="stop", usage={"total_tokens": 7}),
            ]
        )
        self.response = CompletionResponse(content="Hello world", usage={"total_tokens": 7})
        self.requests: list[CompletionRequest] = []
        self.complete_calls = 0
        self.stream_calls = 0
        self.wait = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.iterator_closed = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        self.complete_calls += 1
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.response

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        self.stream_calls += 1
        try:
            for item in self.items:
                if isinstance(item, Exception):
                    raise item
                yield item
            self.entered.set()
            if self.wait:
                await self.release.wait()
        finally:
            self.iterator_closed = True

    async def aclose(self) -> None:
        self.closed = True


def generation(
    provider: ScriptedProvider, *, timeout: float = 60
) -> tuple[FakeJobStore, JobService, JobRunner]:
    store = FakeJobStore()
    registry = JobHandlerRegistry([GenerationJobHandler(lambda: provider, timeout_seconds=timeout)])
    clock = FixedClock(NOW)
    service = JobService(store, FakeJobQueue(store), registry, clock, SequentialIdGenerator())
    return store, service, JobRunner(store, registry, clock)


async def test_stream_opt_in_persists_ordered_tokens_and_exactly_one_completion() -> None:
    provider = ScriptedProvider()
    store, service, runner = generation(provider)
    job = await service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True, "max_tokens": 25}, user_id=OWNER
    )
    await runner.run(job.id)
    await runner.run(job.id)  # Existing terminal-delivery guard.
    finished = await service.get(job.id)
    events = (await store.events_after(job.id, 0, 100)).events
    assert finished.state == JobState.COMPLETED
    assert finished.result_payload == {
        "text": "Hello world",
        "usage": {"total_tokens": 7},
        "finish_reason": "stop",
    }
    assert [e.payload["delta"] for e in events if e.event_type == JobEventType.TOKEN] == [
        "Hello",
        " world",
    ]
    assert [e.sequence_number for e in events] == list(range(1, len(events) + 1))
    completions = [e for e in events if e.event_type == JobEventType.STREAM_COMPLETED]
    assert len(completions) == 1
    assert completions[0].payload["result"] == finished.result_payload
    assert events[-1] == completions[0]
    assert provider.stream_calls == 1 and provider.complete_calls == 0
    assert provider.closed and provider.iterator_closed
    request = provider.requests[0]
    assert request.tools is None
    assert request.model_options is not None and request.model_options.max_tokens == 25


@pytest.mark.parametrize("opt_in", [{}, {"stream": False}])
async def test_non_streaming_preserves_completion_and_emits_only_progress(
    opt_in: dict[str, Any],
) -> None:
    provider = ScriptedProvider()
    store, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello", **opt_in}, user_id=OWNER)
    await runner.run(job.id)
    assert (await service.get(job.id)).result_payload == {
        "text": "Hello world",
        "usage": {"total_tokens": 7},
    }
    assert {event.event_type for event in store.events[job.id]} == {JobEventType.JOB_PROGRESS}
    assert provider.complete_calls == 1 and provider.stream_calls == 0
    assert provider.closed


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": " "},
        {"prompt": "x" * 4001},
        {"prompt": 1},
        {"prompt": "x", "stream": "true"},
        {"prompt": "x", "stream": 1},
        {"prompt": "x", "max_tokens": True},
        {"prompt": "x", "max_tokens": 0},
        {"prompt": "x", "max_tokens": 2049},
        {"prompt": "x", "actor_id": str(OWNER)},
        {"prompt": "x", "tools": []},
    ],
)
async def test_invalid_generation_is_rejected_before_queue_or_provider(
    payload: dict[str, Any],
) -> None:
    provider = ScriptedProvider()
    store, service, _ = generation(provider)
    with pytest.raises(InvariantViolationError):
        await service.submit("llm.generate", payload, user_id=OWNER)
    assert not store.jobs and not provider.requests


@pytest.mark.parametrize(
    "items",
    [
        [StreamChunk(delta="partial"), ProviderUnavailableError("private provider details")],
        [StreamChunk(delta="no finish marker")],
        [StreamChunk(finish_reason="stop")],
        [StreamChunk(delta="blocked", finish_reason="content_filter")],
        [StreamChunk(tool_calls=[ToolCall("call", "finalize_clinical_note", "{}")])],
        [StreamChunk(delta="done", finish_reason="stop"), StreamChunk(delta="extra")],
        [StreamChunk(delta="x" * 65_537, finish_reason="stop")],
    ],
    ids=[
        "provider-failure",
        "truncated",
        "empty",
        "filtered",
        "tool-call",
        "late-text",
        "oversized",
    ],
)
async def test_invalid_or_partial_stream_never_fabricates_success(
    items: list[StreamChunk | Exception],
) -> None:
    provider = ScriptedProvider(items)
    store, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello", "stream": True}, user_id=OWNER)
    await runner.run(job.id)
    failed = await service.get(job.id)
    assert failed.state == JobState.FAILED and failed.result_payload is None
    assert failed.last_error == "JOB_HANDLER_FAILED"
    assert not failed.checkpoint_data
    assert JobEventType.STREAM_COMPLETED not in {e.event_type for e in store.events[job.id]}
    assert store.events[job.id][-1].payload["state"] == "FAILED"
    assert provider.closed and provider.iterator_closed


@pytest.mark.parametrize("stream", [False, True])
async def test_provider_timeout_is_failure_with_cleanup_and_no_completion(stream: bool) -> None:
    provider = ScriptedProvider([StreamChunk(delta="partial")])
    provider.wait = True
    store, service, runner = generation(provider, timeout=0.04)
    job = await service.submit("llm.generate", {"prompt": "Hello", "stream": stream}, user_id=OWNER)
    await asyncio.wait_for(runner.run(job.id), 2)
    assert (await service.get(job.id)).state == JobState.FAILED
    assert provider.closed
    assert not stream or provider.iterator_closed
    assert JobEventType.STREAM_COMPLETED not in {e.event_type for e in store.events[job.id]}


async def test_cancellation_while_waiting_for_provider_stops_without_another_token() -> None:
    provider = ScriptedProvider([StreamChunk(delta="partial")])
    provider.wait = True
    store, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello", "stream": True}, user_id=OWNER)
    running = asyncio.create_task(runner.run(job.id))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        requested = await store.request_cancel(job.id, NOW)
        assert requested.cancellation_requested and requested.state == JobState.STARTED
        await asyncio.wait_for(running, 2)
    finally:
        provider.release.set()
        await asyncio.gather(running, return_exceptions=True)
    cancelled = await service.get(job.id)
    assert cancelled.state == JobState.CANCELLED and cancelled.last_error is None
    assert not cancelled.checkpoint_data and cancelled.result_payload is None
    assert provider.closed and provider.iterator_closed
    assert [e.payload["delta"] for e in store.events[job.id] if e.event_type == "token"] == [
        "partial"
    ]
    assert JobEventType.STREAM_COMPLETED not in {e.event_type for e in store.events[job.id]}


async def test_cancelled_non_streaming_completion_cannot_publish_success() -> None:
    provider = ScriptedProvider()
    provider.wait = True
    store, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello"}, user_id=OWNER)
    running = asyncio.create_task(runner.run(job.id))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        await store.request_cancel(job.id, NOW)
    finally:
        provider.release.set()
        await asyncio.wait_for(running, 2)
    assert (await service.get(job.id)).state == JobState.CANCELLED
    assert provider.closed and not store.jobs[job.id].checkpoint_data


async def test_saved_generation_checkpoint_is_used_without_reopening_provider() -> None:
    provider = ScriptedProvider()
    store, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello", "stream": True}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    saved = {"text": "already generated", "usage": {}, "finish_reason": "stop"}
    await store.checkpoint(job.id, {"generation-v1": saved}, NOW)
    await runner.run(job.id)
    assert (await service.get(job.id)).result_payload == saved
    assert not provider.requests
    assert sum(e.event_type == JobEventType.STREAM_COMPLETED for e in store.events[job.id]) == 1


@pytest.mark.parametrize(
    "response",
    [
        CompletionResponse(content=None),
        CompletionResponse(content="x" * 65_537),
        CompletionResponse(content="text", tool_calls=[ToolCall("call", "tool", "{}")]),
    ],
)
async def test_non_streaming_invalid_provider_response_fails_closed(
    response: CompletionResponse,
) -> None:
    provider = ScriptedProvider()
    provider.response = response
    _, service, runner = generation(provider)
    job = await service.submit("llm.generate", {"prompt": "Hello"}, user_id=OWNER)
    await runner.run(job.id)
    assert (await service.get(job.id)).state == JobState.FAILED
    assert provider.closed
