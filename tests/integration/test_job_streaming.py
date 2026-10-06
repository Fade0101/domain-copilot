"""#21: real PostgreSQL transactions, JWT routes and separate Celery processes."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

from app.application.errors import JobCancelled
from app.core.config import AuthSettings, DatabaseSettings, LLMSettings, QueueSettings, Settings
from app.core.container import Container
from app.domain.auth.value_objects import Role, UserId
from app.domain.jobs.entities import JobState
from app.domain.jobs.events import JobEventType
from app.domain.shared.errors import InvalidStateTransitionError
from app.infrastructure.persistence.job_events import append_event
from app.infrastructure.persistence.job_store import PostgresJobStore, create_job_engine
from app.infrastructure.persistence.models import Base, UserModel
from app.presentation.api import dependencies, security
from app.presentation.api.app import create_app
from tests.integration.test_job_queue import Environment, wait_for, worker
from tests.integration.test_job_queue import database_url as database_url
from tests.integration.test_job_queue import jobs as jobs
from tests.support.sse import Frame, decode_sse, live_stream


@dataclass
class StreamAPI:
    container: Container
    app: FastAPI
    client: httpx.AsyncClient
    tokens: dict[Role, str]
    owners: dict[Role, UUID]
    settings: Settings

    def auth(self, role: Role | None = Role.ANALYST) -> dict[str, str]:
        return {} if role is None else {"Authorization": "Bearer " + self.tokens[role]}


@pytest.fixture
async def api(jobs: Environment, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[StreamAPI]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=jobs.url),
        queue=QueueSettings(
            broker_url=jobs.broker, default_queue=jobs.queue, publish_timeout_seconds=0.5
        ),
        llm=LLMSettings(provider="ollama", fallback=None),
        auth=AuthSettings(
            secret_key=SecretStr("ticket21-test-only-signing-secret"), seed_demo_accounts=False
        ),
    )
    container = Container(settings)
    assert container.database is not None
    # The #20 queue fixture does not authenticate its owner; #21 exercises the
    # actual stored-user contract, which requires a nonempty password digest.
    async with container.database.session_factory() as session, session.begin():
        await session.execute(
            sa.update(UserModel)
            .where(UserModel.id == jobs.owner)
            .values(hashed_password="unused-test-digest")
        )
    tokens, owners = {}, {Role.ANALYST: jobs.owner}
    for role in Role:
        identifier = owners.setdefault(role, uuid4())
        if role != Role.ANALYST:
            async with container.database.session_factory() as session, session.begin():
                session.add(
                    UserModel(
                        id=identifier,
                        email=f"{identifier}@example.com",
                        hashed_password="unused-test-digest",
                        role=role.value,
                    )
                )
        user = await container.user_repository.get_by_id(UserId(str(identifier)))
        assert user is not None
        tokens[role] = container.token_service.issue(
            subject=str(identifier), role=role.value
        ).access_token
    monkeypatch.setattr(dependencies, "get_container", lambda: container)
    monkeypatch.setattr(security, "get_container", lambda: container)
    app = create_app(settings)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield StreamAPI(container, app, client, tokens, owners, settings)
    finally:
        await container.dispose()


@pytest.mark.parametrize(
    "role,expected", [(Role.ANALYST, 200), (Role.ADMIN, 200), (Role.REVIEWER, 403), (None, 401)]
)
async def test_stream_authenticates_and_enforces_stored_ownership(
    api: StreamAPI, jobs: Environment, role: Role | None, expected: int
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    await jobs.runtime.runner.run(job.id)
    response = await api.client.get(f"/api/v1/jobs/{job.id}/stream", headers=api.auth(role))
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-store" in response.headers["cache-control"]
        frames = decode_sse(response.text)
        assert frames[0].data["state"] == "PENDING"
        assert frames[-1].data["state"] == "COMPLETED"
        assert all(frame.event == "job_progress" for frame in frames)


@pytest.mark.parametrize(
    "role,expected", [(Role.ANALYST, 202), (Role.ADMIN, 202), (Role.REVIEWER, 403), (None, 401)]
)
async def test_cancel_authenticates_and_persists_authoritative_request(
    api: StreamAPI, jobs: Environment, role: Role | None, expected: int
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    response = await api.client.post(f"/api/v1/jobs/{job.id}/cancel", headers=api.auth(role))
    assert response.status_code == expected, response.text
    current = await jobs.runtime.service.get(job.id)
    assert current.cancellation_requested == (expected == 202)
    assert current.state == (JobState.CANCELLED if expected == 202 else JobState.QUEUED)
    if expected == 202:
        assert response.json()["cancellation_requested"]
        assert response.json()["state"] == "CANCELLED"
        events = (await jobs.runtime.service.store.events_after(job.id, 0, 100)).events
        assert events[-1].event_type == JobEventType.JOB_PROGRESS
        assert events[-1].payload["state"] == "CANCELLED"


@pytest.mark.parametrize("endpoint", ["stream", "cancel"])
async def test_ids_and_role_payloads_cannot_bypass_job_authorization(
    api: StreamAPI, jobs: Environment, endpoint: str
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    headers = {**api.auth(Role.REVIEWER), "X-Role": "admin"}
    path = f"/api/v1/jobs/{job.id}/{endpoint}?role=admin&user_id={jobs.owner}"
    response = await api.client.request(
        "GET" if endpoint == "stream" else "POST", path, headers=headers
    )
    assert response.status_code == 403
    missing = await api.client.request(
        "GET" if endpoint == "stream" else "POST",
        f"/api/v1/jobs/{uuid4()}/{endpoint}",
        headers=api.auth(Role.ADMIN),
    )
    assert missing.status_code == 404
    assert not (await jobs.runtime.service.get(job.id)).cancellation_requested


@pytest.mark.parametrize("cursor", ["-1", "1.5", "2147483648", "not-an-id"])
async def test_invalid_last_event_id_is_structured_422_before_streaming(
    api: StreamAPI, jobs: Environment, cursor: str
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    response = await api.client.get(
        f"/api/v1/jobs/{job.id}/stream", headers={**api.auth(), "Last-Event-ID": cursor}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("streaming", [False, True])
async def test_real_worker_generation_opt_in_and_durable_replay(
    api: StreamAPI, jobs: Environment, tmp_path: Path, streaming: bool
) -> None:
    with worker(jobs, tmp_path):
        response = await api.client.post(
            "/api/v1/jobs",
            headers=api.auth(Role.ADMIN),
            json={
                "operation_type": "llm.generate",
                "payload": {"prompt": "Hello", "stream": streaming},
            },
        )
        assert response.status_code == 202, response.text
        identifier = UUID(response.json()["job_id"])
        finished = await wait_for(jobs, identifier, JobState.COMPLETED)
        # Terminal redelivery cannot append tokens or another completion.
        await jobs.runtime.runner.run(identifier)
    assert finished.result_payload is not None
    assert finished.result_payload["text"] == "First second"
    response = await api.client.get(
        f"/api/v1/jobs/{identifier}/stream", headers=api.auth(Role.ADMIN)
    )
    assert response.status_code == 200
    frames = decode_sse(response.text)
    assert [f.sequence for f in frames] == sorted({f.sequence for f in frames})
    assert [f.data["state"] for f in frames if f.event == "job_progress"] == [
        "PENDING",
        "QUEUED",
        "STARTED",
        "STARTED",
        "COMPLETED",
    ]
    assert [f.data["delta"] for f in frames if f.event == "token"] == (
        ["First", " second"] if streaming else []
    )
    completions = [f for f in frames if f.event == "stream_completed"]
    assert len(completions) == int(streaming)
    if streaming:
        assert completions[0].data["result"] == finished.result_payload
    cursor = frames[2].sequence
    reconnected = await api.client.get(
        f"/api/v1/jobs/{identifier}/stream",
        headers={**api.auth(Role.ADMIN), "Last-Event-ID": str(cursor)},
    )
    assert decode_sse(reconnected.text) == [f for f in frames if f.sequence > cursor]


async def test_disconnect_releases_only_connection_and_worker_keeps_generating(
    api: StreamAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit(
            "llm.generate", {"prompt": "hold:" + uuid4().hex, "stream": True}, user_id=jobs.owner
        )

        async def assert_committed(frame: Frame) -> None:
            page = await jobs.runtime.service.store.events_after(job.id, frame.sequence - 1, 1)
            assert page.events and page.events[0].sequence_number == frame.sequence
            assert page.events[0].event_type.value == frame.event

        async with live_stream(
            api.app, f"/api/v1/jobs/{job.id}/stream", api.auth(), assert_committed
        ) as stream:
            await stream.wait_for(lambda frames: any(f.event == "token" for f in frames))
            assert stream.status == 200
            last_id = stream.frames[-1].sequence
        # ASGI received http.disconnect. No cancel/resume/queue call is made.
        still_running = await jobs.runtime.service.get(job.id)
        assert still_running.state == JobState.STARTED
        assert not still_running.cancellation_requested
        async with jobs.runtime.service.store.lock(job.id) as active:
            assert active is None
        with jobs.runtime.engine.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO job_test_release(job_id, allowed) VALUES (:id, true)"),
                {"id": job.id},
            )
        completed = await wait_for(jobs, job.id, JobState.COMPLETED)
        assert not completed.cancellation_requested
    response = await api.client.get(
        f"/api/v1/jobs/{job.id}/stream", headers={**api.auth(), "Last-Event-ID": str(last_id)}
    )
    missed = decode_sse(response.text)
    assert missed and all(frame.sequence > last_id for frame in missed)
    assert [f.data["delta"] for f in missed if f.event == "token"] == [" second"]
    assert missed[-1].event == "stream_completed"


async def test_worker_observes_postgres_cancel_during_quiet_provider_io(
    api: StreamAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit(
            "llm.generate", {"prompt": "hold:" + uuid4().hex, "stream": True}, user_id=jobs.owner
        )
        async with live_stream(api.app, f"/api/v1/jobs/{job.id}/stream", api.auth()) as stream:
            await stream.wait_for(lambda frames: any(f.event == "token" for f in frames))
            response = await api.client.post(f"/api/v1/jobs/{job.id}/cancel", headers=api.auth())
            assert response.status_code == 202 and response.json()["cancellation_requested"]
            cancelled = await wait_for(jobs, job.id, JobState.CANCELLED)
            await asyncio.wait_for(stream.task, 5)
            assert stream.frames[-1].data["state"] == "CANCELLED"
            assert all(frame.event != "stream_completed" for frame in stream.frames)
        # One-worker queue proves cooperative exit releases the actual task slot.
        sentinel = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
        await wait_for(jobs, sentinel.id, JobState.COMPLETED)
    assert cancelled.last_error is None and cancelled.result_payload is None
    assert cancelled.cancellation_requested and not cancelled.checkpoint_data


async def test_real_provider_failure_streams_failed_progress_without_completion(
    api: StreamAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit(
            "llm.generate", {"prompt": "fail-after-token", "stream": True}, user_id=jobs.owner
        )
        await wait_for(jobs, job.id, JobState.FAILED)
    response = await api.client.get(f"/api/v1/jobs/{job.id}/stream", headers=api.auth())
    frames = decode_sse(response.text)
    assert [f.data["delta"] for f in frames if f.event == "token"] == ["First"]
    assert frames[-1].data["state"] == "FAILED"
    assert frames[-1].data["error"] == "JOB_HANDLER_FAILED"
    assert not any(f.event == "stream_completed" for f in frames)


async def test_concurrent_public_and_private_writers_share_unique_postgres_sequence(
    jobs: Environment,
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    await store.transition(job.id, JobState.STARTED, now)

    def private_event(index: int) -> None:
        with jobs.runtime.engine.begin() as connection:
            append_event(connection, job.id, "test.private_audit", {"private": index}, now)

    await asyncio.gather(
        *(store.append_token(job.id, str(index), now) for index in range(16)),
        *(asyncio.to_thread(private_event, index) for index in range(16)),
    )
    with jobs.runtime.engine.connect() as connection:
        sequences = list(
            connection.scalars(
                sa.text(
                    "SELECT sequence_number FROM job_events WHERE job_id=:id "
                    "ORDER BY sequence_number"
                ),
                {"id": job.id},
            )
        )
    assert sequences == list(range(1, 36))
    public = await store.events_after(job.id, 0, 100)
    assert len(public.events) == 19  # Three lifecycle events plus sixteen tokens.
    assert all("private" not in event.payload for event in public.events)
    assert len({event.sequence_number for event in public.events}) == 19
    with pytest.raises(IntegrityError):
        with jobs.runtime.engine.begin() as connection:
            connection.execute(
                Base.metadata.tables["job_events"]
                .insert()
                .values(
                    id=uuid4(),
                    job_id=job.id,
                    sequence_number=sequences[-1],
                    event_type="token",
                    payload={"delta": "duplicate"},
                    created_at=now,
                )
            )


async def test_uncommitted_or_rolled_back_event_is_never_replayable(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    await store.transition(job.id, JobState.STARTED, now)
    with jobs.runtime.engine.connect() as connection:
        transaction = connection.begin()
        row = append_event(connection, job.id, "token", {"delta": "committed later"}, now)
        sequence = row["sequence_number"]
        assert not (await store.events_after(job.id, sequence - 1, 100)).events
        transaction.commit()
        assert (await store.events_after(job.id, sequence - 1, 100)).events[0].payload == {
            "delta": "committed later"
        }
        transaction = connection.begin()
        append_event(connection, job.id, "token", {"delta": "rolled back"}, now)
        transaction.rollback()
    assert not (await store.events_after(job.id, sequence, 100)).events


async def test_replay_survives_a_new_process_without_redis(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    await jobs.runtime.runner.run(job.id)
    page = await jobs.runtime.service.store.events_after(job.id, 4, 100)
    script = """
import asyncio, json, os
from uuid import UUID
from app.infrastructure.persistence.job_store import PostgresJobStore, create_job_engine
async def read():
    engine = create_job_engine(os.environ["T21_REPLAY_DATABASE_URL"])
    try:
        page = await PostgresJobStore(engine).events_after(
            UUID(os.environ["T21_REPLAY_JOB_ID"]), 4, 100)
        print(json.dumps([{"sequence": e.sequence_number, "kind": e.event_type.value,
                           "payload": e.payload} for e in page.events]))
    finally:
        engine.dispose()
asyncio.run(read())
"""
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=15,
        creationflags=creation_flags,
        env={
            **os.environ,
            "T21_REPLAY_DATABASE_URL": jobs.url,
            "T21_REPLAY_JOB_ID": str(job.id),
            "QUEUE__BROKER_URL": "redis://127.0.0.1:1/0",
        },
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert json.loads(process.stdout) == [
        {
            "sequence": event.sequence_number,
            "kind": event.event_type.value,
            "payload": event.payload,
        }
        for event in page.events
    ]


async def test_fresh_api_replays_postgres_history_with_unavailable_redis(
    api: StreamAPI, jobs: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    await jobs.runtime.runner.run(job.id)
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        settings = api.settings.model_copy(
            update={
                "queue": QueueSettings(
                    broker_url=f"redis://127.0.0.1:{unavailable.getsockname()[1]}/0",
                    default_queue=jobs.queue,
                    publish_timeout_seconds=0.1,
                )
            }
        )
        fresh = Container(settings)
        monkeypatch.setattr(dependencies, "get_container", lambda: fresh)
        monkeypatch.setattr(security, "get_container", lambda: fresh)
        try:
            response = await api.client.get(
                f"/api/v1/jobs/{job.id}/stream",
                headers={**api.auth(), "Last-Event-ID": "2"},
            )
            assert response.status_code == 200
            frames = decode_sse(response.text)
            assert [f.sequence for f in frames] == [3, 4, 5]
            assert frames[-1].data["state"] == "COMPLETED"
        finally:
            await fresh.dispose()


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED])
@pytest.mark.parametrize("first", ["cancel", "finish"])
async def test_postgres_commit_order_decides_cancel_vs_terminal_transition(
    jobs: Environment, terminal: JobState, first: str
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, now)
        if first == "cancel":
            request = await store.request_cancel(job.id, now)
            assert request.state == JobState.STARTED and request.cancellation_requested
            settled = await store.transition(job.id, terminal, now, result={"ok": True}, error="X")
            assert settled.state == JobState.CANCELLED
        else:
            settled = await store.transition(job.id, terminal, now, result={"ok": True}, error="X")
            assert await store.request_cancel(job.id, now) == settled
            assert not settled.cancellation_requested
    assert (await jobs.runtime.service.get(job.id)) == settled
    assert settled.state == (JobState.CANCELLED if first == "cancel" else terminal)
    events = (await store.events_after(job.id, 0, 100)).events
    completions = [event for event in events if event.event_type == "stream_completed"]
    assert len(completions) == int(settled.state == JobState.COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        await store.transition(job.id, JobState.COMPLETED, now)
    assert (await store.events_after(job.id, 0, 100)).events == events


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED])
async def test_concurrent_cancel_requests_and_terminal_writer_have_one_winner(
    jobs: Environment, terminal: JobState
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, now)
        await asyncio.gather(
            store.transition(job.id, terminal, now, result={"ok": True}, error="X"),
            *(store.request_cancel(job.id, now) for _ in range(12)),
        )
    final = await jobs.runtime.service.get(job.id)
    assert final.state in {terminal, JobState.CANCELLED}
    events = (await store.events_after(job.id, 0, 100)).events
    terminal_events = [
        event
        for event in events
        if event.event_type == "job_progress"
        and event.payload["state"] in {"COMPLETED", "FAILED", "CANCELLED"}
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].payload["state"] == final.state.value
    assert await store.request_cancel(job.id, now) == final
    assert (await store.events_after(job.id, 0, 100)).events == events
    assert final.cancellation_requested == (final.state == JobState.CANCELLED)


async def test_concurrent_idle_cancel_is_one_terminal_result_without_enqueue(
    api: StreamAPI, jobs: Environment
) -> None:
    job = jobs.runtime.service.prepare("diagnostic", {}, user_id=jobs.owner)
    await jobs.runtime.service.store.add(job)
    replies = await asyncio.gather(
        *(api.client.post(f"/api/v1/jobs/{job.id}/cancel", headers=api.auth()) for _ in range(12))
    )
    assert all(
        reply.status_code == 202 and reply.json()["state"] == "CANCELLED" for reply in replies
    )
    assert await jobs.runtime.service.dispatch(job.id) == await jobs.runtime.service.get(job.id)
    events = (await jobs.runtime.service.store.events_after(job.id, 0, 100)).events
    assert [event.payload["state"] for event in events] == ["PENDING", "PENDING", "CANCELLED"]
    assert (await jobs.runtime.service.get(job.id)).attempt_number == 0


async def test_paused_job_cancels_without_requeue_or_checkpoint_loss(
    api: StreamAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        paused = await jobs.runtime.service.submit("test.pause", {}, user_id=jobs.owner)
        await wait_for(jobs, paused.id, JobState.STARTED)
        sentinel = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
        await wait_for(jobs, sentinel.id, JobState.COMPLETED)
        before = await jobs.runtime.service.get(paused.id)
        response = await api.client.post(f"/api/v1/jobs/{paused.id}/cancel", headers=api.auth())
        assert response.status_code == 202 and response.json()["state"] == "CANCELLED"
        assert await jobs.runtime.service.reconcile() == 0
    after = await jobs.runtime.service.get(paused.id)
    assert after.checkpoint_data == before.checkpoint_data
    assert after.attempt_number == before.attempt_number == 1
    assert after.last_error is None


async def test_cancel_at_worker_release_is_atomic_with_advisory_unlock(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, now)
        request = await store.request_cancel(job.id, now)
        assert request.state == JobState.STARTED
        # Exit just like JobPaused, without a handler terminal transition.
    cancelled = await jobs.runtime.service.get(job.id)
    assert cancelled.state == JobState.CANCELLED and cancelled.cancellation_requested
    async with store.lock(job.id) as released:
        assert released is not None


async def test_no_advisory_lock_leaks_when_job_is_missing(jobs: Environment) -> None:
    missing = uuid4()
    async with jobs.runtime.service.store.lock(missing) as first:
        assert first is not None and await first.get(missing) is None
    independent_engine = create_job_engine(jobs.url)
    try:
        async with PostgresJobStore(independent_engine).lock(missing) as second:
            assert second is not None
    finally:
        independent_engine.dispose()


async def test_cancelled_execution_refuses_further_token_or_checkpoint_writes(
    jobs: Environment,
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, now)
        await store.request_cancel(job.id, now)
        with pytest.raises(JobCancelled):
            await store.append_token(job.id, "late", now)
        with pytest.raises(JobCancelled):
            await store.checkpoint(job.id, {"late": {"value": 1}}, now)
    assert not (await jobs.runtime.service.get(job.id)).checkpoint_data
    events = (await store.events_after(job.id, 0, 100)).events
    assert all(event.event_type == "job_progress" for event in events)


async def test_connected_client_receives_new_tokens_after_historical_page(
    api: StreamAPI, jobs: Environment
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    await store.transition(job.id, JobState.STARTED, now)
    first = await store.append_token(job.id, "historical", now)
    async with live_stream(api.app, f"/api/v1/jobs/{job.id}/stream", api.auth()) as stream:
        await stream.wait_for(
            lambda frames: any(f.sequence == first.sequence_number for f in frames)
        )
        second = await store.append_token(job.id, " new", now)
        await stream.wait_for(
            lambda frames: any(f.sequence == second.sequence_number for f in frames)
        )
        await store.transition(job.id, JobState.COMPLETED, now, result={"text": "historical new"})
        await asyncio.wait_for(stream.task, 5)
        assert [f.data["delta"] for f in stream.frames if f.event == "token"] == [
            "historical",
            " new",
        ]
        assert stream.frames[-1].event == "stream_completed"


async def test_interrupted_sql_finishes_before_execution_connection_is_reused(
    jobs: Environment,
) -> None:
    job = await jobs.runtime.service.submit(
        "llm.generate", {"prompt": "Hello", "stream": True}, user_id=jobs.owner
    )
    store = jobs.runtime.service.store
    now = datetime.now(UTC)
    async with store.lock(job.id) as execution:
        assert execution is not None
        await execution.transition(job.id, JobState.STARTED, now)
        with jobs.runtime.engine.connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(sa.text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"), {"id": job.id})
            pending = asyncio.create_task(execution.append_token(job.id, "in flight", now))
            try:
                # Observe the actual PostgreSQL lock wait before interrupting
                # the coroutine that owns the synchronous SQL operation.
                async with asyncio.timeout(3):
                    while True:
                        with jobs.runtime.engine.connect() as observer:
                            waiting = observer.scalar(
                                sa.text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE wait_event_type='Lock' AND query LIKE '%FOR UPDATE%'"
                                )
                            )
                        if waiting:
                            break
                        await asyncio.sleep(0.02)
                pending.cancel()
                await asyncio.sleep(0.02)
                assert not pending.done()
            finally:
                transaction.rollback()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 3)
        await execution.transition(job.id, JobState.FAILED, now, error="JOB_HANDLER_FAILED")
    events = (await store.events_after(job.id, 0, 100)).events
    assert events[-2].payload == {"delta": "in flight"}
    assert events[-1].payload["state"] == "FAILED"
