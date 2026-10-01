"""Ports: durable jobs, exclusive execution, and registered handlers (T7-01/05)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.jobs.entities import Job, JobState


class IJobStore(Protocol):
    async def add(self, job: Job) -> None: ...

    async def get(self, job_id: UUID) -> Job | None: ...

    async def transition(
        self,
        job_id: UUID,
        target: JobState,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job:
        """Atomically validate and persist a lifecycle transition."""
        ...

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        """Commit checkpoint data for a STARTED job; reject any other state."""
        ...

    async def dispatchable(self, limit: int) -> list[UUID]:
        """Return PENDING/QUEUED IDs in creation order, excluding legacy untyped rows."""
        ...

    def lock(self, job_id: UUID) -> AbstractAsyncContextManager[IJobStore | None]:
        """Yield a store under an exclusive execution lock, or None if already held.

        The lock spans checkpoint commits and releases on process/connection loss.
        """
        ...


class IJobContext(Protocol):
    job_id: UUID
    user_id: UUID
    correlation_id: UUID | None
    payload: dict[str, Any]

    async def step(
        self, name: str, action: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Return a saved step result, or execute and durably checkpoint the action."""
        ...


class IJobHandler(Protocol):
    operation_type: str

    def validate(self, payload: dict[str, Any]) -> None:
        """Reject invalid input with InvariantViolationError before job creation."""
        ...

    async def run(self, context: IJobContext) -> dict[str, Any]:
        """Execute resumable steps and return the final JSON result.

        Raise JobPaused to release the worker without completing the logical job,
        or JobCancelled after a cooperative signal. Workflow and cancellation
        transport policy belong to their own tickets.
        """
        ...
