"""SDK-free job doubles for domain/application contract tests."""

from __future__ import annotations

import socket
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.application.errors import (
    JobCancelled,
    JobNotFoundError,
    JobQueueUnavailableError,
    PermissionDeniedError,
    UnknownPrincipalError,
)
from app.application.ports.jobs import IJobStore
from app.application.ports.queue import IJobQueue
from app.domain.auth.value_objects import Permission, Role, UserId
from app.domain.auth.permissions import role_has_permission
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEvent, JobEventPage, JobEventType, progress_payload
from app.domain.jobs.recovery import EXECUTING_PHASES, permits_execution, retry_job
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError


class FakeJobStore(IJobStore):
    def __init__(self) -> None:
        self.jobs: dict[UUID, Job] = {}
        self.transitions: list[JobState] = []
        self.locked: set[UUID] = set()
        self.events: dict[UUID, list[JobEvent]] = {}
        # Fake user store for retry_failed authorisation checks.
        self.users: dict[UUID, Role] = {}

    def event(
        self, job: Job, kind: JobEventType, payload: dict[str, Any], now: datetime
    ) -> JobEvent:
        events = self.events.setdefault(job.id, [])
        event = JobEvent(uuid4(), job.id, len(events) + 1, kind, deepcopy(payload), now)
        events.append(event)
        return event

    async def add(self, job: Job) -> None:
        self.jobs[job.id] = deepcopy(job)
        self.transitions.append(job.state)
        self.event(job, JobEventType.JOB_PROGRESS, progress_payload(job), job.created_at)

    async def add_or_get(self, job: Job) -> tuple[Job, bool]:
        """Atomically accept a canonical submission; return (existing, False) on collision."""
        key = job.idempotency_key or str(job.id)
        for existing in self.jobs.values():
            if (existing.idempotency_key or str(existing.id)) == key:
                return deepcopy(existing), False
        await self.add(job)
        return deepcopy(job), True

    async def claim(self, job_id: UUID, now: datetime, lease_seconds: float) -> Job | None:
        """Claim an eligible job; return None if already owned, terminal, or ineligible."""
        if job_id not in self.jobs:
            return None
        job = self.jobs[job_id]
        if job.terminal or job.state == JobState.PENDING:
            return None
        if job.cancellation_requested:
            job = job.transition(JobState.CANCELLED, now)
            self.jobs[job_id] = job
            return None
        # Phase eligibility (no workflow bindings in the fake, so always eligible).
        if job.paused_at is not None:
            return None
        if job.next_retry_at is not None and job.next_retry_at > now:
            return None
        if job.state == JobState.STARTED and job.lease_owner is not None:
            if job.lease_expires_at is not None and job.lease_expires_at > now:
                return None
            # Expired lease → retry or fail.
            job = retry_job(job, now, due=now, error="JOB_WORKER_LOST")
            self.jobs[job_id] = job
            if job.terminal:
                return None
        if job.state == JobState.QUEUED:
            job = job.transition(JobState.STARTED, now)
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        job = replace(job, lease_owner=owner, lease_acquired_at=now,
                      lease_expires_at=now + timedelta(seconds=lease_seconds))
        self.jobs[job_id] = job
        return deepcopy(job)

    async def heartbeat(self, job_id: UUID, now: datetime) -> None:
        if job_id not in self.jobs:
            return
        job = self.jobs[job_id]
        if job.terminal:
            return
        lease_seconds = 60.0
        self.jobs[job_id] = replace(
            job, lease_expires_at=now + timedelta(seconds=lease_seconds)
        )

    async def pause(self, job_id: UUID, now: datetime) -> None:
        if job_id not in self.jobs:
            return
        job = self.jobs[job_id]
        if job.terminal:
            return
        self.jobs[job_id] = replace(
            job, paused_at=now, lease_owner=None,
            lease_acquired_at=None, lease_expires_at=None, updated_at=now
        )

    async def schedule_retry(
        self, job_id: UUID, now: datetime, due: datetime, *, error: str
    ) -> Job:
        if job_id not in self.jobs:
            raise JobNotFoundError("Job not found.")
        job = self.jobs[job_id]
        if job.terminal:
            return deepcopy(job)
        if job.cancellation_requested:
            return await self.transition(job_id, JobState.CANCELLED, now)
        job = retry_job(job, now, due=due, error=error)
        self.jobs[job_id] = job
        return deepcopy(job)

    async def retry_failed(
        self, job_id: UUID, actor_id: UserId, reason: str, now: datetime
    ) -> Job:
        if job_id not in self.jobs:
            raise JobNotFoundError("Job not found.")
        actor_uuid = UUID(actor_id.value)
        role = self.users.get(actor_uuid)
        if role is None:
            raise UnknownPrincipalError("Retry actor is no longer present.")
        if not role_has_permission(role, Permission.MANAGE_ALL_JOBS):
            raise PermissionDeniedError("Manual retry requires job management permission.")
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 4000:
            raise InvariantViolationError("Retry requires a reason of 3 to 4000 characters.")
        job = retry_job(self.jobs[job_id], now, due=now, error="JOB_MANUAL_RETRY", manual=True)
        self.jobs[job_id] = job
        self.event(job, JobEventType.JOB_PROGRESS, progress_payload(job, manual_retry=True), now)
        return deepcopy(job)

    async def resume(self, job_id: UUID, now: datetime) -> Job:
        if job_id not in self.jobs:
            raise JobNotFoundError("Job not found.")
        job = self.jobs[job_id]
        if job.state != JobState.STARTED:
            raise InvalidStateTransitionError("This job cannot be resumed.")
        if job.cancellation_requested:
            return await self.transition(job_id, JobState.CANCELLED, now)
        if job.paused_at is not None:
            job = replace(job, paused_at=None, updated_at=now)
        elif job.lease_owner is not None:
            job = retry_job(job, now, due=now, error="JOB_WORKER_LOST")
        self.jobs[job_id] = job
        return deepcopy(job)

    async def recoverable(self, now: datetime, limit: int) -> list[UUID]:
        return [
            job.id for job in self.jobs.values()
            if (job.state == JobState.STARTED and job.lease_owner is not None
                and job.lease_expires_at is not None and job.lease_expires_at <= now
                and job.paused_at is None)
        ][:limit]

    async def recover_interrupted(
        self, job_id: UUID, now: datetime, due: datetime
    ) -> Job | None:
        if job_id not in self.jobs:
            return None
        job = self.jobs[job_id]
        if (job.state != JobState.STARTED or job.paused_at is not None
                or job.lease_owner is None or job.lease_expires_at is None
                or job.lease_expires_at > now):
            return None
        if job.cancellation_requested:
            return await self.transition(job_id, JobState.CANCELLED, now)
        job = retry_job(job, now, due=due, error="JOB_WORKER_LOST")
        self.jobs[job_id] = job
        return deepcopy(job)

    async def reserve_dispatch(
        self, job_id: UUID, now: datetime, interval: timedelta, *, force: bool = False
    ) -> Job | None:
        if job_id not in self.jobs:
            return None
        job = self.jobs[job_id]
        if (job.state not in {JobState.PENDING, JobState.QUEUED}
                or job.cancellation_requested
                or (job.next_retry_at is not None and job.next_retry_at > now)):
            return None
        if (not force and job.last_dispatched_at is not None
                and job.last_dispatched_at + interval > now):
            return None
        if job.state == JobState.PENDING:
            job = job.transition(JobState.QUEUED, now)
        job = replace(job, last_dispatched_at=now)
        self.jobs[job_id] = job
        return deepcopy(job)

    async def get(self, job_id: UUID) -> Job | None:
        return deepcopy(self.jobs.get(job_id))

    async def transition(
        self,
        job_id: UUID,
        target: JobState,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job:
        if job_id not in self.jobs:
            raise JobNotFoundError("Job not found.")
        job = self.jobs[job_id].transition(target, now, result=result, error=error)
        self.jobs[job_id] = deepcopy(job)
        self.transitions.append(job.state)
        self.event(job, JobEventType.JOB_PROGRESS, progress_payload(job), now)
        if job.streaming and job.state == JobState.COMPLETED:
            self.event(
                job,
                JobEventType.STREAM_COMPLETED,
                {"state": job.state.value, "result": job.result_payload},
                now,
            )
        return deepcopy(job)

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        if self.jobs[job_id].cancellation_requested:
            raise JobCancelled()
        if self.jobs[job_id].state != JobState.STARTED:
            raise InvalidStateTransitionError("Only a STARTED job may checkpoint.")
        self.jobs[job_id] = replace(
            self.jobs[job_id],
            checkpoint_data=deepcopy(data),
            updated_at=now,
        )
        self.event(
            self.jobs[job_id],
            JobEventType.JOB_PROGRESS,
            progress_payload(self.jobs[job_id], checkpoint_steps=list(data), step_completed=True),
            now,
        )

    async def events_after(self, job_id: UUID, sequence: int, limit: int) -> JobEventPage:
        if job_id not in self.jobs:
            raise JobNotFoundError("Job not found.")
        return JobEventPage(
            deepcopy(self.jobs[job_id]),
            tuple(
                deepcopy(
                    [
                        event
                        for event in self.events.get(job_id, [])
                        if event.sequence_number > sequence
                    ][:limit]
                )
            ),
        )

    async def append_token(self, job_id: UUID, delta: str, now: datetime) -> JobEvent:
        job = self.jobs[job_id]
        if job.cancellation_requested:
            raise JobCancelled()
        if not job.streaming or job.state != JobState.STARTED:
            raise InvalidStateTransitionError("Tokens require an opted-in STARTED job.")
        return self.event(job, JobEventType.TOKEN, {"delta": delta}, now)

    async def request_cancel(self, job_id: UUID, now: datetime) -> Job:
        job = self.jobs[job_id]
        if job.terminal:
            return deepcopy(job)
        requested = job.request_cancel(now)
        self.jobs[job_id] = requested
        if not job.cancellation_requested:
            self.event(requested, JobEventType.JOB_PROGRESS, progress_payload(requested), now)
        if job_id not in self.locked:
            return await self.transition(job_id, JobState.CANCELLED, now)
        return deepcopy(requested)

    async def dispatchable(self, limit: int) -> list[UUID]:
        return [
            job.id
            for job in self.jobs.values()
            if job.state
            in {
                JobState.PENDING,
                JobState.QUEUED,
            }
        ][:limit]

    @asynccontextmanager
    async def lock(self, job_id: UUID) -> AsyncIterator[IJobStore | None]:
        if job_id in self.locked:
            yield None
            return
        self.locked.add(job_id)
        try:
            yield self
        finally:
            job = self.jobs.get(job_id)
            if job is not None and job.cancellation_requested and not job.terminal:
                await self.transition(job_id, JobState.CANCELLED, job.updated_at)
            self.locked.remove(job_id)


class FakeJobQueue(IJobQueue):
    def __init__(self, store: FakeJobStore) -> None:
        self.ids: list[UUID] = []
        self.unavailable = False
        self.store = store

    async def enqueue(self, job_id: UUID) -> None:
        # The broker may deliver immediately: the record must already be durable.
        assert self.store.jobs[job_id].state in {JobState.QUEUED, JobState.STARTED}
        if self.unavailable:
            raise JobQueueUnavailableError("offline")
        self.ids.append(job_id)
