"""Public projections of the existing ordered job history; no transport concerns."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.jobs.entities import Job


class JobEventType(StrEnum):
    TOKEN = "token"
    STREAM_COMPLETED = "stream_completed"
    JOB_PROGRESS = "job_progress"


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: UUID
    job_id: UUID
    sequence_number: int
    event_type: JobEventType
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobEventPage:
    job: Job
    events: tuple[JobEvent, ...]


def progress_payload(job: Job, **details: Any) -> dict[str, Any]:
    return {
        "state": job.state.value,
        "cancellation_requested": job.cancellation_requested,
        "attempt_number": job.attempt_number,
        "error": job.last_error,
        **details,
    }
