"""Shared sequence allocation on #6's job_events, serialized by the parent row."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from app.application.errors import JobNotFoundError
from app.domain.jobs.entities import Job, JobState
from app.domain.jobs.events import JobEventType, progress_payload
from app.infrastructure.persistence.models import Base

_jobs = Base.metadata.tables["jobs"]
_events = Base.metadata.tables["job_events"]


def next_sequence(connection: Connection, job_id: UUID) -> int:
    """Caller keeps this transaction open through INSERT and commit.

    All producers, including approval and evaluation, use this same row lock.
    The existing UNIQUE(job_id, sequence_number) remains the database backstop.
    """
    if (
        connection.scalar(sa.select(_jobs.c.id).where(_jobs.c.id == job_id).with_for_update())
        is None
    ):
        raise JobNotFoundError("Job not found.")
    return (
        int(
            connection.scalar(
                sa.select(sa.func.coalesce(sa.func.max(_events.c.sequence_number), 0)).where(
                    _events.c.job_id == job_id
                )
            )
            or 0
        )
        + 1
    )


def append_event(
    connection: Connection,
    job_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    now: datetime,
) -> RowMapping:
    sequence = next_sequence(connection, job_id)
    return (
        connection.execute(
            _events.insert()
            .values(
                id=uuid4(),
                job_id=job_id,
                sequence_number=sequence,
                event_type=event_type,
                payload=payload,
                created_at=now,
            )
            .returning(_events)
        )
        .mappings()
        .one()
    )


def append_progress(connection: Connection, job: Job, now: datetime, **details: Any) -> None:
    append_event(
        connection, job.id, JobEventType.JOB_PROGRESS, progress_payload(job, **details), now
    )


def append_completion(connection: Connection, job: Job, now: datetime) -> None:
    """Called only in the transaction that first commits COMPLETED."""
    if job.streaming and job.state == JobState.COMPLETED:
        append_event(
            connection,
            job.id,
            JobEventType.STREAM_COMPLETED,
            {"state": job.state.value, "result": job.result_payload},
            now,
        )
