"""Generic checkpointed runner, independent of Celery and the job's domain."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from app.application.errors import JobCancelled, JobNotFoundError, JobPaused, JobStoreError
from app.application.jobs.registry import JobHandlerRegistry, validate_json
from app.application.ports.jobs import IJobContext, IJobStore
from app.application.ports.system import IClock
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvariantViolationError


class JobContext(IJobContext):
    def __init__(
        self, job: Job, store: IJobStore, clock: IClock, max_checkpoint_bytes: int
    ) -> None:
        self.job_id = job.id
        self.user_id = job.user_id
        self.correlation_id = job.correlation_id
        self.payload = deepcopy(job.input_payload)
        self.streaming = job.streaming
        self._checkpoints = deepcopy(job.checkpoint_data)
        self._store = store
        self._clock = clock
        self._limit = max_checkpoint_bytes

    async def check_cancelled(self) -> None:
        job = await self._store.get(self.job_id)
        if job is None:
            raise JobNotFoundError("Job not found.")
        if job.cancellation_requested:
            raise JobCancelled()

    async def emit_token(self, delta: str) -> None:
        await self._store.append_token(self.job_id, delta, self._clock.now())

    async def step(
        self, name: str, action: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        if not name or len(name) > 128:
            raise InvariantViolationError("Checkpoint step names must contain 1 to 128 characters.")
        await self.check_cancelled()
        if name in self._checkpoints:
            return deepcopy(self._checkpoints[name])
        result = await action()
        await self.check_cancelled()
        validate_json(result, self._limit)
        checkpoints = {**self._checkpoints, name: deepcopy(result)}
        validate_json(checkpoints, self._limit)
        await self._store.checkpoint(self.job_id, checkpoints, self._clock.now())
        self._checkpoints = checkpoints
        return deepcopy(result)


class JobRunner:
    def __init__(
        self,
        store: IJobStore,
        handlers: JobHandlerRegistry,
        clock: IClock,
        *,
        max_checkpoint_bytes: int = 1_048_576,
    ) -> None:
        self._store = store
        self._handlers = handlers
        self._clock = clock
        self._checkpoint_limit = max_checkpoint_bytes

    async def run(self, job_id: UUID) -> None:
        async with self._store.lock(job_id) as store:
            if store is None:
                return
            job = await store.get(job_id)
            if job is None:
                raise JobNotFoundError("Job not found.")
            if job.terminal or job.state == JobState.PENDING:
                return
            if job.state == JobState.QUEUED:
                job = await store.transition(job.id, JobState.STARTED, self._clock.now())
            context = JobContext(job, store, self._clock, self._checkpoint_limit)
            try:
                if job.cancellation_requested:
                    raise JobCancelled()
                handler = self._handlers.get(job.operation_type)
                handler.validate(job.input_payload)
                result = await handler.run(context)
                validate_json(result, self._checkpoint_limit)
            except JobPaused:
                return
            except JobCancelled:
                await store.transition(job.id, JobState.CANCELLED, self._clock.now())
                return
            except JobStoreError:
                # Leave STARTED for checkpoint resume after PostgreSQL recovers.
                raise
            except Exception:
                # Handler exception text may contain secrets or sensitive input.
                await store.transition(
                    job.id, JobState.FAILED, self._clock.now(), error="JOB_HANDLER_FAILED"
                )
                return
            await store.transition(job.id, JobState.COMPLETED, self._clock.now(), result=result)
