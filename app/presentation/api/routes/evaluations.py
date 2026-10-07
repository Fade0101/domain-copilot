"""Non-streaming evaluation controls on existing durable T7 jobs."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from app.application.auth.context import Principal
from app.application.evaluation.report import markdown_report
from app.application.evaluation.service import EvaluationService
from app.domain.auth.value_objects import Permission
from app.domain.jobs.entities import Job
from app.presentation.api.dependencies import get_evaluation_service
from app.presentation.api.schemas.jobs import JobAcceptedResponse
from app.presentation.api.security import get_current_principal, require_permission

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _accepted(job: Job, request: Request, response: Response) -> JobAcceptedResponse:
    url = request.url_for("evaluation_status", job_id=str(job.id)).path
    response.headers["Location"] = url
    return JobAcceptedResponse(job_id=job.id, state=job.state, status_url=url)


@router.post("", status_code=202, response_model=JobAcceptedResponse)
async def submit_evaluation(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.MANAGE_ALL_JOBS)),
    service: EvaluationService = Depends(get_evaluation_service),
) -> JobAcceptedResponse:
    """Admin-only submission, with no request body. The server pins its configured catalog.

    Returns a durably accepted job and Location/status_url before evaluation executes.
    """
    return _accepted(await service.submit(principal), request, response)


@router.get("/{job_id}")
async def evaluation_status(
    job_id: UUID,
    principal: Principal = Depends(get_current_principal),
    service: EvaluationService = Depends(get_evaluation_service),
) -> dict[str, Any]:
    return await service.status(job_id, principal)


@router.get("/{job_id}/report", response_model=None)
async def evaluation_report(
    job_id: UUID,
    format: Literal["json", "markdown"] = "json",
    principal: Principal = Depends(get_current_principal),
    service: EvaluationService = Depends(get_evaluation_service),
) -> Any:
    report = await service.report(job_id, principal)
    if format == "markdown":
        return PlainTextResponse(markdown_report(report), media_type="text/markdown")
    return report


@router.post("/{job_id}/cancel", status_code=202, response_model=JobAcceptedResponse)
async def cancel_evaluation(
    job_id: UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.MANAGE_ALL_JOBS)),
    service: EvaluationService = Depends(get_evaluation_service),
) -> JobAcceptedResponse:
    return _accepted(await service.cancel(job_id, principal), request, response)


@router.post("/{job_id}/restart", status_code=202, response_model=JobAcceptedResponse)
async def restart_evaluation(
    job_id: UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.MANAGE_ALL_JOBS)),
    service: EvaluationService = Depends(get_evaluation_service),
) -> JobAcceptedResponse:
    return _accepted(await service.restart(job_id, principal), request, response)
