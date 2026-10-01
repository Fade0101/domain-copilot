"""Submission commits to PostgreSQL before best-effort broker dispatch (T7-02/04)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from app.application.errors import JobNotFoundError, JobQueueUnavailableError
from app.application.jobs.registry import JobHandlerRegistry, validate_json
from app.application.ports.jobs import IJobStore
from app.application.ports.queue import IJobQueue
from app.application.ports.system import IClock, IIdGenerator
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError


class JobService:
    def __init__(
        self,
        store: IJobStore,
        queue: IJobQueue,
        handlers: JobHandlerRegistry,
        clock: IClock,
        identifiers: IIdGenerator,
        *,
        max_payload_bytes: int = 65_536,
    ) -> None:
        self.store = store
        self._queue = queue
        self._handlers = handlers
        self._clock = clock
        self._ids = identifiers
        self._max_payload_bytes = max_payload_bytes

    async def submit(
        self,
        operation_type: str,
        payload: dict[str, Any],
        *,
        user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Job:
        validate_json(payload, self._max_payload_bytes)
        self._handlers.get(operation_type).validate(payload)
        now = self._clock.now()
        job = Job(
            id=UUID(self._ids.new_id()),
            operation_type=operation_type,
            input_payload=deepcopy(payload),
            created_at=now,
            updated_at=now,
            user_id=user_id,
            correlation_id=correlation_id or UUID(self._ids.new_id()),
        )
        await self.store.add(job)
        # Mark QUEUED before publishing: a fast worker must never see PENDING.
        job = await self.store.transition(job.id, JobState.QUEUED, self._clock.now())
        try:
            await self._queue.enqueue(job.id)
        except JobQueueUnavailableError:
            # Accepted durably. Reconciliation republishes the same ID; do not
            # return 503 and invite the caller to create a second logical job.
            pass
        return job

    async def get(self, job_id: UUID) -> Job:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError("Job not found.")
        return job

    async def reconcile(self, *, limit: int = 100) -> int:
        """Explicitly republish pending/queued work after lost enqueue or Redis loss.

        No STARTED jobs are selected: waiting for clinical approval is not a crash.
        Ticket 22 owns scheduling, backoff, and phase-aware recovery policy.
        """
        if not 1 <= limit <= 1000:
            raise InvariantViolationError("Reconciliation limit must be between 1 and 1000.")
        published = 0
        for job_id in await self.store.dispatchable(limit):
            job = await self.get(job_id)
            if job.state == JobState.PENDING:
                try:
                    job = await self.store.transition(job.id, JobState.QUEUED, self._clock.now())
                except InvalidStateTransitionError:
                    job = await self.get(job_id)
            if job.state == JobState.QUEUED:
                await self._queue.enqueue(job.id)
                published += 1
        return published

    async def resume(self, job_id: UUID) -> None:
        """Explicit operator redelivery of an interrupted STARTED job without resetting it.

        The operator must distinguish interruption from deliberate workflow pauses.
        The runner's execution lock and checkpoints protect repeated deliveries.
        """
        job = await self.get(job_id)
        if job.state != JobState.STARTED:
            raise InvalidStateTransitionError("Only a STARTED job can be resumed.")
        await self._queue.enqueue(job.id)
