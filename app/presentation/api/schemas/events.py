"""JSON data contracts carried inside SSE frames."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.jobs.entities import JobState


class AskTokenData(BaseModel):
    delta: str
    trace_id: UUID


class JobEventData(BaseModel):
    job_id: UUID
    created_at: datetime


class JobTokenData(JobEventData):
    delta: str


class JobProgressData(JobEventData):
    """Additional public progress details vary by operation; never private audit payloads."""

    model_config = ConfigDict(extra="allow")
    state: JobState
    cancellation_requested: bool
    attempt_number: int
    error: str | None


class JobStreamCompletedData(JobEventData):
    state: JobState
    result: dict[str, Any] | None
