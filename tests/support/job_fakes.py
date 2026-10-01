"""SDK-free job doubles for domain/application contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.errors import JobNotFoundError, JobQueueUnavailableError
from app.application.ports.jobs import IJobStore
from app.application.ports.queue import IJobQueue
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError


class FakeJobStore(IJobStore):
    def __init__(self) -> None:
        self.jobs: dict[UUID, Job] = {}
        self.transitions: list[JobState] = []
        self.locked: set[UUID] = set()

    async def add(self, job: Job) -> None:
        self.jobs[job.id] = deepcopy(job)
        self.transitions.append(job.state)

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
        self.transitions.append(target)
        return deepcopy(job)

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        if self.jobs[job_id].state != JobState.STARTED:
            raise InvalidStateTransitionError("Only a STARTED job may checkpoint.")
        self.jobs[job_id] = replace(
            self.jobs[job_id],
            checkpoint_data=deepcopy(data),
            updated_at=now,
        )

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
