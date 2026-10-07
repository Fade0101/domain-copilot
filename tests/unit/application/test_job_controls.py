"""Job controls use stored actors and preserve the existing lifecycle."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import ConfigurationError, ResourceOwnershipError, UnknownPrincipalError
from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.domain.auth.value_objects import ResourceType, Role, UserId
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError
from tests.support.fakes import (
    FakeOwnershipQuery,
    FakeUserRepository,
    FixedClock,
    SequentialIdGenerator,
    build_user,
)
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

NOW = datetime(2026, 10, 6, tzinfo=UTC)
OWNER = UUID(int=100)
ACTOR = UUID(int=200)


def controls(
    role: Role = Role.ANALYST, actor_id: UUID = OWNER
) -> tuple[FakeJobStore, JobService, FakeOwnershipQuery, FakeUserRepository, Principal]:
    user = build_user(user_id=str(actor_id), email="actor@example.com", role=role)
    users = FakeUserRepository([user])
    ownership = FakeOwnershipQuery()
    store = FakeJobStore()
    registry = JobHandlerRegistry([DiagnosticJobHandler()])
    service = JobService(
        store,
        FakeJobQueue(store),
        registry,
        FixedClock(NOW),
        SequentialIdGenerator(),
        authorization=AuthorizationService(ownership),
        users=users,
    )
    return store, service, ownership, users, Principal.from_user(user)


@pytest.mark.parametrize("command", ["events", "cancel"])
@pytest.mark.parametrize(
    "role,actor_id,allowed",
    [
        (Role.ANALYST, OWNER, True),
        (Role.REVIEWER, OWNER, True),
        (Role.ADMIN, ACTOR, True),
        (Role.ANALYST, ACTOR, False),
        (Role.REVIEWER, ACTOR, False),
    ],
)
async def test_application_controls_enforce_owner_or_admin(
    command: str, role: Role, actor_id: UUID, allowed: bool
) -> None:
    store, service, ownership, _, principal = controls(role, actor_id)
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), UserId(str(OWNER)))
    before = await store.get(job.id)
    action = (
        service.events_after(job.id, 0, principal)
        if command == "events"
        else service.cancel(job.id, principal)
    )
    if not allowed:
        with pytest.raises(ResourceOwnershipError):
            await action
        assert await store.get(job.id) == before
    else:
        await action
        assert (await service.get(job.id)).state == (
            JobState.CANCELLED if command == "cancel" else JobState.QUEUED
        )


@pytest.mark.parametrize("command", ["events", "cancel"])
async def test_forged_or_stale_admin_role_cannot_grant_cross_owner_access(command: str) -> None:
    _, service, ownership, _, principal = controls(Role.ANALYST, ACTOR)
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), UserId(str(OWNER)))
    forged = replace(principal, role=Role.ADMIN)
    with pytest.raises(ResourceOwnershipError):
        if command == "events":
            await service.events_after(job.id, 0, forged)
        else:
            await service.cancel(job.id, forged)


async def test_deleted_actor_cannot_cancel_via_internal_service() -> None:
    store, service, ownership, users, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), UserId(str(OWNER)))
    users.users.clear()
    with pytest.raises(UnknownPrincipalError):
        await service.cancel(job.id, principal)
    assert not store.jobs[job.id].cancellation_requested


async def test_missing_authorization_dependencies_fail_closed() -> None:
    store, _, _, _, principal = controls()
    service = JobService(
        store,
        FakeJobQueue(store),
        JobHandlerRegistry([DiagnosticJobHandler()]),
        FixedClock(NOW),
        SequentialIdGenerator(),
    )
    with pytest.raises(ConfigurationError):
        await service.cancel(UUID(int=1), principal)
    with pytest.raises(ConfigurationError):
        await service.events_after(UUID(int=1), 0, principal)


@pytest.mark.parametrize("target", [JobState.COMPLETED, JobState.FAILED])
async def test_committed_cancel_wins_over_later_success_or_failure(target: JobState) -> None:
    store, service, _, _, _ = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, NOW)
        await store.request_cancel(job.id, NOW)
        settled = await store.transition(
            job.id, target, NOW, result={"ok": True}, error="private failure"
        )
    assert settled.state == JobState.CANCELLED
    assert settled.last_error is None and settled.result_payload is None


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED])
async def test_cancel_is_noop_after_a_committed_terminal_state(terminal: JobState) -> None:
    store, service, _, _, _ = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    before = await store.transition(job.id, terminal, NOW)
    events = list(store.events[job.id])
    assert await store.request_cancel(job.id, NOW) == before
    assert store.events[job.id] == events
    with pytest.raises(InvalidStateTransitionError):
        await store.transition(job.id, JobState.COMPLETED, NOW)


async def test_queued_cancel_is_idempotent_and_later_delivery_does_no_work() -> None:
    store, service, ownership, _, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), principal.user_id)
    cancelled = await service.cancel(job.id, principal)
    events = list(store.events[job.id])
    assert await service.cancel(job.id, principal) == cancelled
    runner = JobRunner(store, JobHandlerRegistry([DiagnosticJobHandler()]), FixedClock(NOW))
    await runner.run(job.id)
    assert store.events[job.id] == events
    assert store.jobs[job.id].attempt_number == 0
    assert not store.jobs[job.id].checkpoint_data


@pytest.mark.parametrize("sequence", [-1, 2_147_483_648, True, "3"])
async def test_event_cursor_is_validated_in_service(sequence: int) -> None:
    _, service, ownership, _, principal = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    ownership.register(ResourceType.JOB, str(job.id), principal.user_id)
    with pytest.raises(InvariantViolationError):
        await service.events_after(job.id, sequence, principal)


async def test_cancel_at_pause_release_does_not_need_a_resume_or_new_task() -> None:
    store, service, _, _, _ = controls()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    async with store.lock(job.id):
        await store.transition(job.id, JobState.STARTED, NOW)
        await store.checkpoint(job.id, {"review": {"state": "AWAITING_APPROVAL"}}, NOW)
        assert (await store.request_cancel(job.id, NOW)).state == JobState.STARTED
    settled = await service.get(job.id)
    assert settled.state == JobState.CANCELLED
    assert settled.checkpoint_data == {"review": {"state": "AWAITING_APPROVAL"}}
