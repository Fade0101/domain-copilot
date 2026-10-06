"""Ports: durable jobs, exclusive execution, and registered handlers (T7-01/05)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.domain.auth.value_objects import UserId
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEvent, JobEventPage


class IJobStore(Protocol):
    async def add(self, job: Job) -> None: ...

    async def add_or_get(self, job: Job) -> tuple[Job, bool]:
        """Atomically accept a canonical submission; only its creator dispatches."""
        ...

    async def claim(self, job_id: UUID, now: datetime, lease_seconds: float) -> Job | None:
        """Under lock(), atomically claim eligible execution and persist its owner."""
        ...

    async def heartbeat(self, job_id: UUID, now: datetime) -> None: ...

    async def pause(self, job_id: UUID, now: datetime) -> None: ...

    async def schedule_retry(
        self, job_id: UUID, now: datetime, due: datetime, *, error: str
    ) -> Job: ...

    async def retry_failed(self, job_id: UUID, actor_id: UserId, reason: str, now: datetime) -> Job:
        """Recheck stored admin authority; commit retry and immutable audit together."""
        ...

    async def resume(self, job_id: UUID, now: datetime) -> Job:
        """Under lock(), resume an eligible crash/pause; never AWAITING_APPROVAL."""
        ...

    async def recoverable(self, now: datetime, limit: int) -> list[UUID]: ...

    async def recover_interrupted(
        self, job_id: UUID, now: datetime, due: datetime
    ) -> Job | None: ...

    async def reserve_dispatch(
        self, job_id: UUID, now: datetime, interval: timedelta, *, force: bool = False
    ) -> Job | None:
        """Commit QUEUED and reserve publication; filter persisted phase and retry due time."""
        ...

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

    async def events_after(self, job_id: UUID, sequence: int, limit: int) -> JobEventPage:
        """Read committed public events in order, with state read before the page."""
        ...

    async def append_token(self, job_id: UUID, delta: str, now: datetime) -> JobEvent:
        """Append only for an opted-in STARTED job; lock and check cancellation."""
        ...

    async def request_cancel(self, job_id: UUID, now: datetime) -> Job:
        """Persist cancellation; settle idle jobs under the existing execution lock.

        Active workers stop cooperatively. A committed terminal state is a no-op.
        """
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
    streaming: bool

    async def check_cancelled(self) -> None:
        """Read the authoritative flag; raise JobCancelled when requested."""
        ...

    async def emit_token(self, delta: str) -> None:
        """Persist a token before any observer may receive it."""
        ...

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
