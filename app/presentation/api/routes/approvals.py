"""Human decisions on existing /runs resources; policy lives in ApprovalService."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.application.approvals.service import ApprovalService
from app.application.auth.context import Principal
from app.presentation.api.dependencies import get_approval_service
from app.presentation.api.schemas.approvals import (
    ApproveRequest,
    DecisionResponse,
    EditAndApproveRequest,
    RejectRequest,
    ReviewResponse,
)
from app.presentation.api.security import get_current_principal

router = APIRouter(prefix="/runs", tags=["approvals"])


@router.get("/{workflow_id}/approval", response_model=ReviewResponse)
async def review_draft(
    workflow_id: UUID,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> ReviewResponse:
    record = await service.get_review(workflow_id, principal)
    response.headers["Cache-Control"] = "no-store"
    return ReviewResponse.from_record(record)


@router.post("/{workflow_id}/approval/approve", response_model=DecisionResponse)
async def approve_draft(
    workflow_id: UUID,
    body: ApproveRequest,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    result = await service.approve(workflow_id, body.draft_id, principal)
    response.headers["Cache-Control"] = "no-store"
    return DecisionResponse.from_result(result)


@router.post("/{workflow_id}/approval/reject", response_model=DecisionResponse)
async def reject_draft(
    workflow_id: UUID,
    body: RejectRequest,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    result = await service.reject(workflow_id, body.draft_id, body.reason, principal)
    response.headers["Cache-Control"] = "no-store"
    return DecisionResponse.from_result(result)


@router.post("/{workflow_id}/approval/edit-and-approve", response_model=DecisionResponse)
async def edit_and_approve_draft(
    workflow_id: UUID,
    body: EditAndApproveRequest,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    result = await service.edit_and_approve(workflow_id, body.draft_id, body.edited_note, principal)
    response.headers["Cache-Control"] = "no-store"
    return DecisionResponse.from_result(result)
