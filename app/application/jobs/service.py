"""Submission commits to PostgreSQL before best-effort broker dispatch (T7-02/04)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import (
    ConfigurationError,
    JobNotFoundError,
    JobQueueUnavailableError,
    UnknownPrincipalError,
)
from app.application.jobs.registry import JobHandlerRegistry, validate_json
from app.application.ports.jobs import IJobStore
from app.application.ports.queue import IJobQueue
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock, IIdGenerator
from app.domain.auth.value_objects import ResourceType
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEventPage
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
        authorization: AuthorizationService | None = None,
        users: IUserRepository | None = None,
    ) -> None:
        self.store = store
        self._queue = queue
        self._handlers = handlers
        self._clock = clock
        self._ids = identifiers
        self._max_payload_bytes = max_payload_bytes
        self._authorization = authorization
        self._users = users

    async def _authorize_observation(self, job_id: UUID, principal: Principal) -> None:
        if self._authorization is None or self._users is None:
            raise ConfigurationError("Authenticated job controls require a user repository.")
        user = await self._users.get_by_id(principal.user_id)
        if user is None:
            raise UnknownPrincipalError("Job actor is no longer present.")
        await self._authorization.require_resource_access(
            Principal.from_user(user), ResourceType.JOB, str(job_id)
        )

    async def events_after(
        self, job_id: UUID, sequence: int, principal: Principal, *, limit: int = 100
    ) -> JobEventPage:
        await self._authorize_observation(job_id, principal)
        if type(sequence) is not int or not 0 <= sequence <= 2_147_483_647:
            raise InvariantViolationError("Last-Event-ID must be a non-negative sequence number.")
        if not 1 <= limit <= 1000:
            raise InvariantViolationError("Event page size must be between 1 and 1000.")
        return await self.store.events_after(job_id, sequence, limit)

    async def cancel(self, job_id: UUID, principal: Principal) -> Job:
        """Owner/admin command, independent of event consumers and broker availability."""
        await self._authorize_observation(job_id, principal)
        return await self.store.request_cancel(job_id, self._clock.now())

    async def submit(
        self,
        operation_type: str,
        payload: dict[str, Any],
        *,
        user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Job:
        job = self.prepare(operation_type, payload, user_id=user_id, correlation_id=correlation_id)
        await self.store.add(job)
        return await self.dispatch(job.id)

    def prepare(
        self,
        operation_type: str,
        payload: dict[str, Any],
        *,
        user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Job:
        """Validate and construct a PENDING job for an atomic domain+job transaction."""
        validate_json(payload, self._max_payload_bytes)
        self._handlers.get(operation_type).validate(payload)
        now = self._clock.now()
        return Job(
            id=UUID(self._ids.new_id()),
            operation_type=operation_type,
            input_payload=deepcopy(payload),
            created_at=now,
            updated_at=now,
            user_id=user_id,
            correlation_id=correlation_id or UUID(self._ids.new_id()),
        )

    async def dispatch(self, job_id: UUID) -> Job:
        """Publish an already committed job; safe for duplicate submissions and recovery."""
        job = await self.get(job_id)
        # Mark QUEUED before publishing: a fast worker must never see PENDING.
        if job.state == JobState.PENDING:
            try:
                job = await self.store.transition(job.id, JobState.QUEUED, self._clock.now())
            except InvalidStateTransitionError:
                job = await self.get(job.id)
        if job.state != JobState.QUEUED:
            return job
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
