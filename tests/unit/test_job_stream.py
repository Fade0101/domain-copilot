"""SSE framing and connection teardown have no authority to control execution."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.application.errors import JobStoreError
from app.domain.auth.value_objects import ResourceType
from app.domain.jobs.entities import JobState
from app.domain.jobs.events import JobEvent, JobEventType
from app.domain.shared.errors import InvariantViolationError
from app.presentation.api import job_stream
from app.presentation.api.job_stream import encode_event, last_sequence, stream_events
from tests.unit.application.test_job_controls import NOW, OWNER, controls


@pytest.mark.parametrize(
    "header,expected", [(None, 0), ("", 0), ("0", 0), ("5", 5), ("2147483647", 2_147_483_647)]
)
def test_last_event_id_accepts_standard_sequence(header: str | None, expected: int) -> None:
    assert last_sequence(header) == expected


@pytest.mark.parametrize("header", ["-1", "1.5", "abc", " 5", "+5", "٥", "2147483648", "9" * 30])
def test_last_event_id_rejects_invalid_cursor(header: str) -> None:
    with pytest.raises(InvariantViolationError):
        last_sequence(header)


def test_sse_frame_preserves_unicode_and_prevents_newline_event_injection() -> None:
    delta = "Hi\n\nevent: stream_completed\r\nid: 999\nمرحبا"
    event = JobEvent(uuid4(), uuid4(), 6, JobEventType.TOKEN, {"delta": delta}, datetime.now(UTC))
    frame = encode_event(event)
    lines = frame.splitlines()
    assert lines[0:2] == ["id: 6", "event: token"]
    assert len(lines) == 4 and lines[-1] == ""
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["delta"] == delta
    assert payload["job_id"] == str(event.job_id)
    assert payload["created_at"] == event.created_at.isoformat()


def connection() -> tuple[Request, asyncio.Queue[dict[str, Any]]]:
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    return Request({"type": "http"}, receive=messages.get), messages


async def test_disconnect_does_not_cancel_and_reconnect_reads_missed_events() -> None:
    store, service, ownership, _, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), principal.user_id)
    page = await service.events_after(job.id, 0, principal)
    request, disconnected = connection()
    stream = stream_events(request, service, job.id, principal, 0, page)
    assert await anext(stream) == ": connected\n\n"
    assert "id: 1" in await anext(stream)
    await disconnected.put({"type": "http.disconnect"})
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not store.jobs[job.id].cancellation_requested
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.COMPLETED, NOW, result={"ok": True})
    replay_request, _ = connection()
    replay = stream_events(
        replay_request,
        service,
        job.id,
        principal,
        1,
        await service.events_after(job.id, 1, principal),
    )
    frames = [frame async for frame in replay]
    assert [frame.splitlines()[0] for frame in frames[1:]] == ["id: 2", "id: 3", "id: 4"]
    assert '"state":"COMPLETED"' in frames[-1]
    assert not store.jobs[job.id].cancellation_requested


async def test_terminal_stream_drains_multiple_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_stream, "PAGE_SIZE", 2)
    store, service, ownership, _, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), principal.user_id)
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.COMPLETED, NOW)
    request, _ = connection()
    frames = [
        frame
        async for frame in stream_events(
            request,
            service,
            job.id,
            principal,
            0,
            await service.events_after(job.id, 0, principal, limit=2),
        )
    ]
    assert [frame.splitlines()[0] for frame in frames[1:]] == ["id: 1", "id: 2", "id: 3", "id: 4"]


@pytest.mark.parametrize("failure", ["storage", "deleted-actor"])
async def test_mid_stream_failure_closes_without_success_or_cancellation(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(job_stream, "POLL_SECONDS", 0)
    store, service, ownership, users, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), principal.user_id)
    page = await service.events_after(job.id, 2, principal)
    if failure == "storage":

        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise JobStoreError("offline")

        monkeypatch.setattr(store, "events_after", unavailable)
    else:
        users.users.clear()
    request, _ = connection()
    frames = [frame async for frame in stream_events(request, service, job.id, principal, 2, page)]
    assert frames == [": connected\n\n"]
    assert not store.jobs[job.id].cancellation_requested
    assert store.jobs[job.id].state == JobState.QUEUED
