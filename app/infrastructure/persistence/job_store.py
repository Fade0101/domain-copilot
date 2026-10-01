"""PostgreSQL job store, with session locks spanning checkpoint commits (T7).

Blocking database calls run off the event loop. The connection holding an
execution lock also performs that execution's reads and writes, so losing it
stops checkpointing instead of silently writing under a lost lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.application.errors import ConfigurationError, JobNotFoundError, JobStoreError
from app.application.ports.jobs import IJobStore
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError
from app.infrastructure.persistence.models import Base

_T = TypeVar("_T")
# Share the mapping used by Alembic and ORM readers; a second table declaration
# can drift from the schema while passing isolated runner tests.
_jobs = Base.metadata.tables["jobs"]


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


class PostgresJobStore(IJobStore):
    def __init__(self, engine: Engine, *, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    async def _call(self, action: Callable[[], _T]) -> _T:
        try:
            return await asyncio.to_thread(action)
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
                connection.execute(
                    _jobs.insert().values(
                        id=job.id,
                        user_id=job.user_id,
                        operation_type=job.operation_type,
                        state=job.state.value,
                        idempotency_key=str(job.id),
                        input_payload=job.input_payload,
                        result_payload=job.result_payload,
                        checkpoint_data=job.checkpoint_data,
                        correlation_id=job.correlation_id,
                        attempt_number=job.attempt_number,
                        max_attempts=3,
                        last_error=job.last_error,
                        created_at=job.created_at,
                        updated_at=job.updated_at,
                        started_at=job.started_at,
                        completed_at=job.completed_at,
                        cancellation_requested=job.cancellation_requested,
                    )
                )

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
                row = (
                    connection.execute(
                        sa.select(_jobs).where(_jobs.c.id == job_id).with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise JobNotFoundError("Job not found.")
                job = _to_job(row).transition(target, now, result=result, error=error)
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
                return job

        return await self._call(update)

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        def update() -> None:
            with self._transaction() as connection:
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

    @asynccontextmanager
    async def lock(self, job_id: UUID) -> AsyncIterator[IJobStore | None]:
        key = int.from_bytes(job_id.bytes[:8], "big", signed=True)

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

        connection, acquired = await self._call(acquire)

        def release() -> None:
            try:
                if acquired and not connection.invalidated:
                    connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    connection.commit()
            except SQLAlchemyError:
                connection.invalidate()
                raise
            finally:
                connection.close()

        try:
            yield PostgresJobStore(self._engine, connection=connection) if acquired else None
        finally:
            await self._call(release)
