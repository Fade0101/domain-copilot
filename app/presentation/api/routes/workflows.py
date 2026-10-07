"""Thin HTTP wiring for #17 submission/resume and #18's persisted final note.

The UI never calls a tool or writes a workflow state. These routes delegate to
the same registered job handler, approval gate and ownership service as workers.
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response

from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import FinalizeClinicalNoteOutput
from app.application.jobs.service import JobService
from app.application.workflow.contracts import CLINICAL_WORKFLOW_OPERATION
from app.application.workflow.service import ClinicalWorkflowService
from app.domain.auth.value_objects import Permission
from app.presentation.api.dependencies import get_job_service, get_workflow_service
from app.presentation.api.schemas.workflows import (
    StartWorkflowRequest,
    WorkflowAcceptedResponse,
    WorkflowStatusResponse,
)
from app.presentation.api.security import get_current_principal, require_permission

router = APIRouter(prefix="/runs", tags=["workflows"])


@router.post("", status_code=202, response_model=WorkflowAcceptedResponse)
async def start_workflow(
    body: StartWorkflowRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permission(Permission.RUN_WORKFLOW)),
    jobs: JobService = Depends(get_job_service),
) -> WorkflowAcceptedResponse:
    """Start the registered clinical workflow. The worker creates its durable run."""
    workflow_id = uuid4()
    job = await jobs.submit(
        CLINICAL_WORKFLOW_OPERATION,
        {**body.model_dump(), "workflow_id": str(workflow_id)},
        user_id=UUID(principal.user_id.value),
    )
    status_url = request.url_for("read_job", job_id=str(job.id)).path
    response.headers["Location"] = status_url
    response.headers["Cache-Control"] = "no-store"
    return WorkflowAcceptedResponse(
        workflow_id=workflow_id, job_id=job.id, state=job.state, status_url=status_url
    )


@router.get("/{workflow_id}/status", response_model=WorkflowStatusResponse)
async def workflow_status(
    workflow_id: UUID,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ClinicalWorkflowService = Depends(get_workflow_service),
) -> WorkflowStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    return WorkflowStatusResponse.from_run(await service.get_workflow(workflow_id, principal))


@router.post("/{workflow_id}/resume", status_code=202, response_class=Response)
async def resume_workflow(
    workflow_id: UUID,
    principal: Principal = Depends(get_current_principal),
    service: ClinicalWorkflowService = Depends(get_workflow_service),
) -> Response:
    """Request guarded finalization; requires a persisted APPROVED decision."""
    await service.resume_workflow(workflow_id, principal)
    return Response(status_code=202, headers={"Cache-Control": "no-store"})


@router.get("/{workflow_id}/note", response_model=FinalizeClinicalNoteOutput)
async def finalized_note(
    workflow_id: UUID,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ClinicalWorkflowService = Depends(get_workflow_service),
) -> FinalizeClinicalNoteOutput:
    """Return the immutable final row; an approved draft alone returns 404."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get_final_note(workflow_id, principal)
