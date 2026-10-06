"""SDK-free job doubles for domain/application contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.application.errors import JobCancelled, JobNotFoundError, JobQueueUnavailableError
from app.application.ports.jobs import IJobStore
from app.application.ports.queue import IJobQueue
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEvent, JobEventPage, JobEventType, progress_payload
from app.domain.shared.errors import InvalidStateTransitionError


class FakeJobStore(IJobStore):
    def __init__(self) -> None:
        self.jobs: dict[UUID, Job] = {}
        self.transitions: list[JobState] = []
        self.locked: set[UUID] = set()
        self.events: dict[UUID, list[JobEvent]] = {}

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
