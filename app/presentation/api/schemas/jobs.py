"""Validated submissions and public job projections; inputs/checkpoints stay private."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.jobs.entities import Job, JobState
from app.presentation.api.schemas.auth import ResourceAccessResponse


class SubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_type: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    payload: dict[str, Any] = Field(default_factory=dict)


class JobAcceptedResponse(BaseModel):
    job_id: UUID
    state: JobState
    status_url: str


class RetryJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=3, max_length=4000)


class JobStatusResponse(ResourceAccessResponse):
    """Extend Ticket 5's ownership projection with the actual PostgreSQL state."""

    job_id: UUID
    operation_type: str
    state: JobState
    result: dict[str, Any] | None
    error: str | None
    correlation_id: UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancellation_requested: bool

    @classmethod
    def from_job(cls, job: Job) -> JobStatusResponse:
        return cls(
            resource_type="job",
            resource_id=str(job.id),
            owner_id=str(job.user_id),
            job_id=job.id,
            operation_type=job.operation_type,
            state=job.state,
            result=job.result_payload,
            error=job.last_error,
            correlation_id=job.correlation_id,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            cancellation_requested=job.cancellation_requested,
        )


class JobListResponse(BaseModel):
    items: list[JobStatusResponse]
    limit: int
    offset: int
