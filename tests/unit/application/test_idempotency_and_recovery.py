"""Unit tests for Ticket #22: idempotency key, retry policy, and recovery rules.

These tests cover the pure-domain and application-layer logic introduced in #22
without requiring a database connection. Integration-level lease/claim scenarios
are covered by tests/integration/test_job_queue.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.errors import PermissionDeniedError, UnknownPrincipalError
from app.application.jobs.identity import submission_key
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.service import JobService
from app.domain.auth.value_objects import Role, UserId
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.recovery import (
    EXECUTING_PHASES,
    JobRetryPolicy,
    permits_execution,
    retry_job,
)
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError
from tests.support.fakes import FixedClock, SequentialIdGenerator
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

NOW = datetime(2026, 10, 6, tzinfo=UTC)
OWNER = UUID(int=1)
ACTOR = UUID(int=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(
    state: JobState = JobState.QUEUED,
    attempt: int = 1,
    max_attempts: int = 4,
    cancellation_requested: bool = False,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    next_retry_at: datetime | None = None,
) -> Job:
    return Job(
        id=UUID(int=99),
        operation_type="diagnostic",
        input_payload={},
        user_id=OWNER,
        created_at=NOW,
        updated_at=NOW,
        state=state,
        attempt_number=attempt,
        max_attempts=max_attempts,
        cancellation_requested=cancellation_requested,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        next_retry_at=next_retry_at,
    )


def _setup() -> tuple[FakeJobStore, JobService]:
    from app.application.jobs.diagnostic import DiagnosticJobHandler
    store = FakeJobStore()
    queue = FakeJobQueue(store)
    registry = JobHandlerRegistry([DiagnosticJobHandler()])
    service = JobService(store, queue, registry, FixedClock(NOW), SequentialIdGenerator())
    return store, service


# ---------------------------------------------------------------------------
# 1. Submission key (identity.py)
# ---------------------------------------------------------------------------

def test_submission_key_is_deterministic() -> None:
    key_a = submission_key("process", {"drug": "aspirin"}, "1", OWNER)
    key_b = submission_key("process", {"drug": "aspirin"}, "1", OWNER)
    assert key_a == key_b


def test_submission_key_differs_by_operation() -> None:
    a = submission_key("process", {}, "1", OWNER)
    b = submission_key("other", {}, "1", OWNER)
    assert a != b


def test_submission_key_differs_by_version() -> None:
    a = submission_key("process", {}, "1", OWNER)
    b = submission_key("process", {}, "2", OWNER)
    assert a != b


def test_submission_key_differs_by_owner() -> None:
    a = submission_key("process", {}, "1", OWNER)
    b = submission_key("process", {}, "1", UUID(int=99))
    assert a != b


def test_submission_key_differs_by_payload() -> None:
    a = submission_key("process", {"drug": "aspirin"}, "1", OWNER)
    b = submission_key("process", {"drug": "ibuprofen"}, "1", OWNER)
    assert a != b


def test_submission_key_has_expected_prefix() -> None:
    key = submission_key("op", {}, "1", OWNER)
    assert key.startswith("job-v1:")


# ---------------------------------------------------------------------------
# 2. Retry policy (recovery.py)
# ---------------------------------------------------------------------------

def test_retry_policy_first_attempt_uses_base_delay() -> None:
    policy = JobRetryPolicy(backoff_base_seconds=0.5, backoff_multiplier=2.0)
    assert policy.delay(1) == timedelta(seconds=0.5)


def test_retry_policy_second_attempt_doubles_delay() -> None:
    policy = JobRetryPolicy(backoff_base_seconds=0.5, backoff_multiplier=2.0)
    assert policy.delay(2) == timedelta(seconds=1.0)


def test_retry_policy_caps_at_max_backoff() -> None:
    policy = JobRetryPolicy(
        backoff_base_seconds=1.0, backoff_multiplier=10.0, max_backoff_seconds=5.0
    )
    assert policy.delay(100) == timedelta(seconds=5.0)


# ---------------------------------------------------------------------------
# 3. permits_execution (recovery.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", list(EXECUTING_PHASES))
def test_permits_execution_for_all_executing_phases(phase: str) -> None:
    assert permits_execution(phase)


def test_permits_execution_for_none_phase() -> None:
    assert permits_execution(None)


@pytest.mark.parametrize("phase", ["AWAITING_APPROVAL", "REJECTED", "COMPLETED", "UNKNOWN"])
def test_denies_execution_for_non_executing_phases(phase: str) -> None:
    assert not permits_execution(phase)


# ---------------------------------------------------------------------------
# 4. retry_job (recovery.py)
# ---------------------------------------------------------------------------

def test_retry_job_from_started_produces_queued() -> None:
    job = _job(state=JobState.STARTED, attempt=1, max_attempts=4)
    retried = retry_job(job, NOW, due=NOW + timedelta(seconds=5), error="CRASH")
    assert retried.state == JobState.QUEUED
    assert retried.next_retry_at == NOW + timedelta(seconds=5)
    assert retried.last_error == "CRASH"
    assert retried.lease_owner is None


def test_retry_job_exhausted_produces_failed() -> None:
    job = _job(state=JobState.STARTED, attempt=4, max_attempts=4)
    result = retry_job(job, NOW, due=NOW, error="CRASH")
    assert result.state == JobState.FAILED
    assert result.last_error == "JOB_RETRIES_EXHAUSTED"


def test_retry_job_manual_from_failed_grants_one_extra_attempt() -> None:
    job = _job(state=JobState.FAILED, attempt=4, max_attempts=4)
    retried = retry_job(job, NOW, due=NOW, error="JOB_MANUAL_RETRY", manual=True)
    assert retried.state == JobState.QUEUED
    assert retried.max_attempts == 5  # One extra granted.


def test_retry_job_refuses_cancelled_job() -> None:
    job = _job(state=JobState.STARTED, cancellation_requested=True)
    with pytest.raises(InvalidStateTransitionError):
        retry_job(job, NOW, due=NOW, error="CRASH")


def test_retry_job_manual_requires_failed_state() -> None:
    job = _job(state=JobState.STARTED)
    with pytest.raises(InvalidStateTransitionError):
        retry_job(job, NOW, due=NOW, error="manual", manual=True)


# ---------------------------------------------------------------------------
# 5. FakeJobStore.add_or_get — idempotency
# ---------------------------------------------------------------------------

async def test_add_or_get_accepts_first_submission() -> None:
    store, service = _setup()
    job = service.prepare("diagnostic", {}, user_id=OWNER)
    accepted, is_new = await store.add_or_get(job)
    assert is_new
    assert accepted.id == job.id


async def test_add_or_get_returns_existing_on_duplicate_key() -> None:
    store, service = _setup()
    job = service.prepare("diagnostic", {}, user_id=OWNER)
    first, _ = await store.add_or_get(job)
    # Simulate re-submission with same idempotency key.
    duplicate = service.prepare("diagnostic", {}, user_id=OWNER)
    duplicate_with_key = __import__("dataclasses").replace(
        duplicate, idempotency_key=first.idempotency_key or str(first.id)
    )
    second, is_new = await store.add_or_get(duplicate_with_key)
    assert not is_new
    assert second.id == first.id


# ---------------------------------------------------------------------------
# 6. FakeJobStore.recoverable + recover_interrupted
# ---------------------------------------------------------------------------

async def test_recoverable_selects_expired_leased_started_jobs() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    # Manually put it in STARTED with an expired lease.
    from dataclasses import replace as dreplace
    now_plus_2 = NOW + timedelta(seconds=2)
    expired_lease = NOW - timedelta(seconds=1)
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        state=JobState.STARTED,
        lease_owner="dead-worker",
        lease_acquired_at=NOW,
        lease_expires_at=expired_lease,
    )
    ids = await store.recoverable(now_plus_2, 100)
    assert job.id in ids


async def test_recoverable_excludes_paused_jobs() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        state=JobState.STARTED,
        lease_owner="dead-worker",
        lease_acquired_at=NOW,
        lease_expires_at=NOW - timedelta(seconds=1),
        paused_at=NOW,
    )
    ids = await store.recoverable(NOW + timedelta(seconds=2), 100)
    assert job.id not in ids


async def test_recover_interrupted_transitions_to_queued() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    from dataclasses import replace as dreplace
    expired_at = NOW - timedelta(seconds=1)
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        state=JobState.STARTED,
        lease_owner="dead-worker",
        lease_acquired_at=NOW,
        lease_expires_at=expired_at,
    )
    recovered = await store.recover_interrupted(
        job.id, NOW + timedelta(seconds=2), NOW + timedelta(seconds=5)
    )
    assert recovered is not None
    assert recovered.state == JobState.QUEUED
    assert recovered.next_retry_at == NOW + timedelta(seconds=5)


async def test_recover_interrupted_respects_cancellation() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        state=JobState.STARTED,
        lease_owner="dead-worker",
        lease_acquired_at=NOW,
        lease_expires_at=NOW - timedelta(seconds=1),
        cancellation_requested=True,
    )
    result = await store.recover_interrupted(job.id, NOW + timedelta(seconds=2), NOW)
    assert result is not None
    assert result.state == JobState.CANCELLED


# ---------------------------------------------------------------------------
# 7. FakeJobStore.retry_failed — manual retry RBAC
# ---------------------------------------------------------------------------

async def test_retry_failed_requires_manage_jobs_permission() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.FAILED, NOW, error="CRASH")
    # Register actor without MANAGE_ALL_JOBS.
    actor_id = UserId(str(ACTOR))
    store.users[ACTOR] = Role.ANALYST
    with pytest.raises(PermissionDeniedError):
        await store.retry_failed(job.id, actor_id, "retrying now", NOW)


async def test_retry_failed_unknown_actor_raises() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.FAILED, NOW, error="CRASH")
    with pytest.raises(UnknownPrincipalError):
        await store.retry_failed(job.id, UserId(str(ACTOR)), "retrying", NOW)


async def test_retry_failed_admin_queues_job() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.FAILED, NOW, error="CRASH")
    store.users[ACTOR] = Role.ADMIN
    retried = await store.retry_failed(job.id, UserId(str(ACTOR)), "operator retry", NOW)
    assert retried.state == JobState.QUEUED


async def test_retry_failed_reason_must_be_at_least_3_chars() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    await store.transition(job.id, JobState.FAILED, NOW, error="CRASH")
    store.users[ACTOR] = Role.ADMIN
    with pytest.raises(InvariantViolationError):
        await store.retry_failed(job.id, UserId(str(ACTOR)), "ab", NOW)


# ---------------------------------------------------------------------------
# 8. FakeJobStore.reserve_dispatch
# ---------------------------------------------------------------------------

async def test_reserve_dispatch_transitions_pending_to_queued() -> None:
    store, service = _setup()
    job = service.prepare("diagnostic", {}, user_id=OWNER)
    await store.add(job)  # Stays PENDING.
    reserved = await store.reserve_dispatch(
        job.id, NOW, timedelta(seconds=30)
    )
    assert reserved is not None
    assert reserved.state == JobState.QUEUED
    assert reserved.last_dispatched_at == NOW


async def test_reserve_dispatch_rate_limits_redelivery() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)  # Already QUEUED.
    first = await store.reserve_dispatch(job.id, NOW, timedelta(seconds=30))
    assert first is not None
    # Second call within interval should be suppressed.
    second = await store.reserve_dispatch(
        job.id, NOW + timedelta(seconds=10), timedelta(seconds=30)
    )
    assert second is None


async def test_reserve_dispatch_force_bypasses_interval() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.reserve_dispatch(job.id, NOW, timedelta(seconds=30))
    forced = await store.reserve_dispatch(
        job.id, NOW + timedelta(seconds=5), timedelta(seconds=30), force=True
    )
    assert forced is not None


async def test_reserve_dispatch_skips_cancelled_jobs() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.request_cancel(job.id, NOW)
    result = await store.reserve_dispatch(job.id, NOW, timedelta(seconds=30))
    assert result is None


# ---------------------------------------------------------------------------
# 9. FakeJobStore.pause + resume
# ---------------------------------------------------------------------------

async def test_pause_clears_lease_and_sets_paused_at() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        lease_owner="worker-1",
        lease_acquired_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=60),
    )
    await store.pause(job.id, NOW)
    paused = await store.get(job.id)
    assert paused.paused_at == NOW
    assert paused.lease_owner is None


async def test_resume_from_paused_clears_paused_at() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(store.jobs[job.id], paused_at=NOW)
    resumed = await store.resume(job.id, NOW + timedelta(seconds=10))
    assert resumed.paused_at is None
    assert resumed.state == JobState.STARTED


async def test_resume_cancels_if_flagged() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    await store.transition(job.id, JobState.STARTED, NOW)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(
        store.jobs[job.id], paused_at=NOW, cancellation_requested=True
    )
    result = await store.resume(job.id, NOW + timedelta(seconds=1))
    assert result.state == JobState.CANCELLED


async def test_resume_non_started_raises() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    with pytest.raises(InvalidStateTransitionError):
        await store.resume(job.id, NOW)  # QUEUED, not STARTED.


# ---------------------------------------------------------------------------
# 10. Heartbeat
# ---------------------------------------------------------------------------

async def test_heartbeat_extends_lease_expiry() -> None:
    store, service = _setup()
    job = await service.submit("diagnostic", {}, user_id=OWNER)
    from dataclasses import replace as dreplace
    store.jobs[job.id] = dreplace(
        store.jobs[job.id],
        state=JobState.STARTED,
        lease_owner="worker-1",
        lease_acquired_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=5),
    )
    await store.heartbeat(job.id, NOW + timedelta(seconds=30))
    updated = await store.get(job.id)
    assert updated.lease_expires_at > NOW + timedelta(seconds=5)
