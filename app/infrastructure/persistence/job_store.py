"""PostgreSQL job store, with session locks spanning checkpoint commits (T7).

Blocking database calls run off the event loop. The connection holding an
execution lock also performs that execution's reads and writes, so losing it
stops checkpointing instead of silently writing under a lost lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.application.errors import ConfigurationError, JobCancelled, JobNotFoundError, JobStoreError
from app.application.ports.jobs import IJobStore
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEvent, JobEventPage, JobEventType
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError
from app.infrastructure.persistence.job_events import (
    append_completion,
    append_event,
    append_progress,
)
from app.infrastructure.persistence.models import Base

_T = TypeVar("_T")
# Share the mapping used by Alembic and ORM readers; a second table declaration
# can drift from the schema while passing isolated runner tests.
_jobs = Base.metadata.tables["jobs"]
_events = Base.metadata.tables["job_events"]


def create_job_engine(database_url: str) -> Engine:
    try:
        url = make_url(database_url.strip().replace("postgres://", "postgresql://", 1))
        if url.drivername not in {"postgresql", "postgresql+psycopg", "postgresql+asyncpg"}:
            raise ConfigurationError("Jobs require PostgreSQL with the psycopg driver.")
        return sa.create_engine(
            url.set(drivername="postgresql+psycopg"),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5},
        )
    except (SQLAlchemyError, ValueError):
        raise ConfigurationError("Invalid job database configuration.") from None


def _to_job(row: RowMapping) -> Job:
    return Job(
        id=row["id"],
        user_id=row["user_id"],
        operation_type=row["operation_type"] or "legacy",
        input_payload=row["input_payload"],
        state=JobState(row["state"]),
        result_payload=row["result_payload"],
        checkpoint_data=row["checkpoint_data"],
        correlation_id=row["correlation_id"],
        last_error=row["last_error"],
        attempt_number=row["attempt_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        cancellation_requested=row["cancellation_requested"],
    )


def job_from_mapping(row: RowMapping) -> Job:
    """Reuse the T7 mapping in domain transactions such as approval rejection."""
    return _to_job(row)


def job_execution_lock_key(job_id: UUID) -> int:
    """Shared advisory-lock identity for workers and atomic approval decisions."""
    return int.from_bytes(job_id.bytes[:8], "big", signed=True)


def job_insert_values(job: Job) -> dict[str, Any]:
    """Shared by job submission and atomic document+source+job acceptance (#8)."""
    return {
        "id": job.id,
        "user_id": job.user_id,
        "operation_type": job.operation_type,
        "state": job.state.value,
        "idempotency_key": str(job.id),
        "input_payload": job.input_payload,
        "result_payload": job.result_payload,
        "checkpoint_data": job.checkpoint_data,
        "correlation_id": job.correlation_id,
        "attempt_number": job.attempt_number,
        "max_attempts": 3,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "cancellation_requested": job.cancellation_requested,
    }


def _locked_job(connection: Connection, job_id: UUID) -> Job:
    row = (
        connection.execute(sa.select(_jobs).where(_jobs.c.id == job_id).with_for_update())
        .mappings()
        .first()
    )
    if row is None:
        raise JobNotFoundError("Job not found.")
    return _to_job(row)


def persist_transition(
    connection: Connection,
    job_id: UUID,
    target: JobState,
    now: datetime,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    **progress: Any,
) -> Job:
    current = _locked_job(connection, job_id)
    job = current.transition(target, now, result=result, error=error)
    connection.execute(
        _jobs.update()
        .where(_jobs.c.id == job_id)
        .values(
            state=job.state.value,
            updated_at=job.updated_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            attempt_number=job.attempt_number,
            last_error=job.last_error,
            result_payload=job.result_payload,
        )
    )
    append_progress(connection, job, now, **progress)
    append_completion(connection, job, now)
    return job


def persist_cancellation(connection: Connection, job_id: UUID, now: datetime) -> Job:
    """An executing worker keeps STARTED until it cooperatively stops.

    An idle/queued/paused job can settle immediately without publishing a task.
    try-lock never waits for the execution lock while holding the row lock.
    """
    current = _locked_job(connection, job_id)
    if current.terminal:
        return current
    job = current.request_cancel(now)
    if not current.cancellation_requested:
        connection.execute(
            _jobs.update()
            .where(_jobs.c.id == job_id)
            .values(cancellation_requested=True, updated_at=now)
        )
        append_progress(connection, job, now)
    idle = connection.scalar(
        sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": job_execution_lock_key(job_id)},
    )
    if idle:
        return persist_transition(connection, job_id, JobState.CANCELLED, now)
    return job


class PostgresJobStore(IJobStore):
    def __init__(self, engine: Engine, *, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    async def _call(self, action: Callable[[], _T]) -> _T:
        pending = asyncio.create_task(asyncio.to_thread(action))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A provider timeout or disconnected HTTP request cannot interrupt
            # synchronous SQL. Drain its transaction before reusing/closing the
            # execution connection; never leave a write running behind it.
            await asyncio.gather(pending, return_exceptions=True)
            raise
        except SQLAlchemyError:
            # Driver exceptions may contain connection strings, SQL parameters,
            # or job payloads. Neither the public error nor its chain exposes them.
            raise JobStoreError("Job storage is unavailable.") from None

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self._connection is not None:
            if self._connection.invalidated:
                raise JobStoreError("Job execution connection was lost.")
            with self._connection.begin():
                yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    async def add(self, job: Job) -> None:
        def insert() -> None:
            with self._transaction() as connection:
                connection.execute(_jobs.insert().values(**job_insert_values(job)))
                append_progress(connection, job, job.created_at)

        await self._call(insert)

    async def get(self, job_id: UUID) -> Job | None:
        def select() -> Job | None:
            with self._transaction() as connection:
                row = (
                    connection.execute(sa.select(_jobs).where(_jobs.c.id == job_id))
                    .mappings()
                    .first()
                )
                return _to_job(row) if row is not None else None

        return await self._call(select)

    async def transition(
        self,
        job_id: UUID,
        target: JobState,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job:
        def update() -> Job:
            with self._transaction() as connection:
                return persist_transition(
                    connection, job_id, target, now, result=result, error=error
                )

        return await self._call(update)

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        def update() -> None:
            with self._transaction() as connection:
                job = _locked_job(connection, job_id)
                if job.cancellation_requested:
                    raise JobCancelled()
                if job.state != JobState.STARTED:
                    raise InvalidStateTransitionError("Only a STARTED job may checkpoint.")
                result = connection.execute(
                    _jobs.update()
                    .where(
                        _jobs.c.id == job_id,
                        _jobs.c.state == JobState.STARTED.value,
                    )
                    .values(checkpoint_data=data, updated_at=now)
                )
                if result.rowcount != 1:
                    raise InvalidStateTransitionError("Only a STARTED job may checkpoint.")
                append_progress(
                    connection, job, now, checkpoint_steps=list(data), step_completed=True
                )

        await self._call(update)

    async def dispatchable(self, limit: int) -> list[UUID]:
        def select() -> list[UUID]:
            with self._transaction() as connection:
                return list(
                    connection.execute(
                        sa.select(_jobs.c.id)
                        .where(
                            _jobs.c.state.in_([JobState.PENDING.value, JobState.QUEUED.value]),
                            _jobs.c.operation_type.is_not(None),
                        )
                        .order_by(_jobs.c.created_at, _jobs.c.id)
                        .limit(limit)
                    ).scalars()
                )

        return await self._call(select)

    async def events_after(self, job_id: UUID, sequence: int, limit: int) -> JobEventPage:
        def read() -> JobEventPage:
            with self._transaction() as connection:
                # Read state FIRST. Seeing a committed terminal state guarantees
                # its atomic final events are visible to the following query.
                row = (
                    connection.execute(sa.select(_jobs).where(_jobs.c.id == job_id))
                    .mappings()
                    .first()
                )
                if row is None:
                    raise JobNotFoundError("Job not found.")
                rows = (
                    connection.execute(
                        sa.select(_events)
                        .where(
                            _events.c.job_id == job_id,
                            _events.c.sequence_number > sequence,
                            _events.c.event_type.in_([kind.value for kind in JobEventType]),
                        )
                        .order_by(_events.c.sequence_number)
                        .limit(limit)
                    )
                    .mappings()
                    .all()
                )
                return JobEventPage(
                    _to_job(row),
                    tuple(
                        JobEvent(
                            item["id"],
                            item["job_id"],
                            item["sequence_number"],
                            JobEventType(item["event_type"]),
                            item["payload"],
                            item["created_at"],
                        )
                        for item in rows
                    ),
                )

        return await self._call(read)

    async def append_token(self, job_id: UUID, delta: str, now: datetime) -> JobEvent:
        if not isinstance(delta, str) or not delta or len(delta.encode("utf-8")) > 65_536:
            raise InvariantViolationError("A token delta must contain 1 to 65536 UTF-8 bytes.")

        def append() -> JobEvent:
            with self._transaction() as connection:
                job = _locked_job(connection, job_id)
                if job.cancellation_requested:
                    raise JobCancelled()
                if not job.streaming or job.state != JobState.STARTED:
                    raise InvalidStateTransitionError("Tokens require an opted-in STARTED job.")
                row = append_event(connection, job_id, JobEventType.TOKEN, {"delta": delta}, now)
                return JobEvent(
                    row["id"],
                    job_id,
                    row["sequence_number"],
                    JobEventType.TOKEN,
                    row["payload"],
                    row["created_at"],
                )

        return await self._call(append)

    async def request_cancel(self, job_id: UUID, now: datetime) -> Job:
        def cancel() -> Job:
            with self._transaction() as connection:
                return persist_cancellation(connection, job_id, now)

        return await self._call(cancel)

    @asynccontextmanager
    async def lock(self, job_id: UUID) -> AsyncIterator[IJobStore | None]:
        key = job_execution_lock_key(job_id)

        def acquire() -> tuple[Connection, bool]:
            connection = self._engine.connect()
            try:
                acquired = bool(
                    connection.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
                )
                connection.commit()
                return connection, acquired
            except BaseException:
                connection.close()
                raise

        acquisition = asyncio.create_task(self._call(acquire))
        try:
            connection, acquired = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            connection, _ = await acquisition
            # Acquisition may already have succeeded. Discard the session so
            # an advisory lock cannot leak into the connection pool.
            await asyncio.to_thread(connection.invalidate)
            await asyncio.to_thread(connection.close)
            raise

        def release() -> None:
            try:
                if acquired and not connection.invalidated:
                    # Serialize the final cancellation check AND unlock with
                    # requests. This closes the JobPaused/check/unlock race:
                    # a later requester sees an idle lock and settles itself.
                    with connection.begin():
                        row = (
                            connection.execute(
                                sa.select(_jobs).where(_jobs.c.id == job_id).with_for_update()
                            )
                            .mappings()
                            .first()
                        )
                        job = _to_job(row) if row is not None else None
                        if job is not None and job.cancellation_requested and not job.terminal:
                            persist_transition(
                                connection, job_id, JobState.CANCELLED, datetime.now(UTC)
                            )
                        connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            except BaseException:
                connection.invalidate()
                raise
            finally:
                connection.close()

        try:
            yield PostgresJobStore(self._engine, connection=connection) if acquired else None
        finally:
            await self._call(release)
