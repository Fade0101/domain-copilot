"""Durable 202 submission and owner-authorized polling (Ticket 20)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import JobNotFoundError
from app.application.jobs.service import JobService
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.jobs.entities import JobState
from app.presentation.api.dependencies import get_job_service
from app.presentation.api.job_stream import PAGE_SIZE, last_sequence, stream_events
from app.presentation.api.schemas.jobs import (
    JobAcceptedResponse,
    JobListResponse,
    JobStatusResponse,
    RetryJobRequest,
    SubmitJobRequest,
)
from app.presentation.api.security import (
    get_authorization_service,
    get_current_principal,
    require_permission,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    state: JobState | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    service: JobService = Depends(get_job_service),
) -> JobListResponse:
    """Analyst/reviewer: own jobs; admin: all owned jobs. Filtered before pagination."""
    jobs = await service.list_jobs(principal, state=state, limit=limit, offset=offset)
    return JobListResponse(
        items=[JobStatusResponse.from_job(job) for job in jobs], limit=limit, offset=offset
    )


@router.post("", status_code=202, response_model=JobAcceptedResponse)
async def submit_job(
    body: SubmitJobRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.MANAGE_ALL_JOBS)),
    service: JobService = Depends(get_job_service),
) -> JobAcceptedResponse:
    """Submit a registered operation as an admin; feature routes authorize their own actions."""
    job = await service.submit(
        body.operation_type,
        body.payload,
        user_id=UUID(principal.user_id.value),
    )
    status_url = request.url_for("read_job", job_id=str(job.id)).path
    response.headers["Location"] = status_url
    return JobAcceptedResponse(job_id=job.id, state=job.state, status_url=status_url)


@router.get("/{job_id}", response_model=JobStatusResponse)
async def read_job(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
    authorization: AuthorizationService = Depends(get_authorization_service),
    service: JobService = Depends(get_job_service),
) -> JobStatusResponse:
    await authorization.require_resource_access(principal, ResourceType.JOB, job_id)
    try:
        identifier = UUID(job_id)
    except ValueError:
        raise JobNotFoundError("Job not found.") from None
    return JobStatusResponse.from_job(await service.get(identifier))


@router.get("/{job_id}/events", response_class=StreamingResponse)
@router.get("/{job_id}/stream", response_class=StreamingResponse)
async def stream_job(
    job_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_current_principal),
    service: JobService = Depends(get_job_service),
) -> StreamingResponse:
    """Owner/admin read-only SSE. /stream is the compatible alias of /events.

    Last-Event-ID resumes after a committed sequence (0..2147483647).
    Disconnect never cancels a job. Stored role/ownership is rechecked on every page.
    """
    sequence = last_sequence(last_event_id)
    # Authenticate, authorize and read the initial page BEFORE sending HTTP 200.
    page = await service.events_after(job_id, sequence, principal, limit=PAGE_SIZE)
    return StreamingResponse(
        stream_events(request, service, job_id, principal, sequence, page),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel", status_code=202, response_model=JobStatusResponse)
async def cancel_job(
    job_id: UUID,
    principal: Principal = Depends(get_current_principal),
    service: JobService = Depends(get_job_service),
) -> JobStatusResponse:
    """Owner/admin cooperative cancellation; terminal jobs are unchanged."""
    return JobStatusResponse.from_job(await service.cancel(job_id, principal))


@router.post("/{job_id}/retry", status_code=202, response_model=JobAcceptedResponse)
async def retry_job(
    job_id: UUID,
    body: RetryJobRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.MANAGE_ALL_JOBS)),
    service: JobService = Depends(get_job_service),
) -> JobAcceptedResponse:
    """Admin only. Audited retry of an eligible FAILED job; never bypasses approval.

    Preserves job identity, checkpoints and accounting. Conflicting state, cancellation
    or an active execution lock returns 409. A duplicate retry after acceptance is 409.
    """
    job = await service.retry(job_id, body.reason, principal)
    url = request.url_for("read_job", job_id=str(job.id)).path
    response.headers["Location"] = url
    return JobAcceptedResponse(job_id=job.id, state=job.state, status_url=url)
