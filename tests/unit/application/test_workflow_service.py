"""Unit tests for ClinicalWorkflowService (Ticket #17)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.workflow.service import ClinicalWorkflowService
from app.domain.approvals.entities import ApprovalDecision, ApprovalStatus
from app.domain.shared.errors import ApprovalRequiredError
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState
from tests.unit.application.test_workflow_safety_order import (
    InMemoryUserRepo,
    InMemoryWorkflowRepo,
    make_test_decision,
    make_test_user,
)


class FakeOwnershipQuery:
    def __init__(self, owner_id: UUID | None = None) -> None:
        self.owner_id = owner_id

    async def owner_of(self, resource_type: Any, resource_id: str) -> UUID | None:
        return self.owner_id


class FakeJobService:
    def __init__(self) -> None:
        self.resumed_job_ids: list[UUID] = []

    async def resume(self, job_id: UUID) -> None:
        self.resumed_job_ids.append(job_id)


class FakeApprovalService:
    def __init__(self, decision: ApprovalDecision | None = None) -> None:
        self.decision = decision

    async def get_decision(
        self, workflow_id: UUID, principal: Principal
    ) -> ApprovalDecision | None:
        return self.decision


@pytest.mark.asyncio
async def test_get_workflow_by_id() -> None:
    """Read workflow requires authenticated user and WORKFLOW:READ permission."""
    user = make_test_user()
    principal = Principal.from_user(user)
    workflow_id = uuid4()
    now = datetime.now(UTC)

    workflow = WorkflowRun(
        id=workflow_id,
        user_id=UUID(str(user.id)),
        correlation_id=str(workflow_id),
        case_summary="Case summary",
        state=ClinicalWorkflowState.RESEARCH,
        approval_job_id=uuid4(),
        created_at=now,
        updated_at=now,
    )
    repo = InMemoryWorkflowRepo()
    await repo.save(workflow)

    service = ClinicalWorkflowService(
        workflow_repo=repo,
        jobs=FakeJobService(),  # type: ignore[arg-type]
        approvals=FakeApprovalService(),  # type: ignore[arg-type]
        users=InMemoryUserRepo(user),
        authorization=AuthorizationService(FakeOwnershipQuery(user.id)),  # type: ignore[arg-type]
    )

    found = await service.get_workflow(workflow_id, principal)
    assert found.id == workflow_id
    assert found.state == ClinicalWorkflowState.RESEARCH


@pytest.mark.asyncio
async def test_resume_workflow_requires_approved_decision() -> None:
    """Resuming a paused workflow requires a persisted APPROVED decision from #19."""
    user = make_test_user()
    principal = Principal.from_user(user)
    workflow_id = uuid4()
    job_id = uuid4()
    now = datetime.now(UTC)

    workflow = WorkflowRun(
        id=workflow_id,
        user_id=UUID(str(user.id)),
        correlation_id=str(workflow_id),
        case_summary="Case summary",
        state=ClinicalWorkflowState.AWAITING_APPROVAL,
        approval_job_id=job_id,
        created_at=now,
        updated_at=now,
    )
    repo = InMemoryWorkflowRepo()
    await repo.save(workflow)

    fake_jobs = FakeJobService()
    # Case 1: No approval decision exists -> raises ApprovalRequiredError
    service_unapproved = ClinicalWorkflowService(
        workflow_repo=repo,
        jobs=fake_jobs,  # type: ignore[arg-type]
        approvals=FakeApprovalService(decision=None),  # type: ignore[arg-type]
        users=InMemoryUserRepo(user),
        authorization=AuthorizationService(FakeOwnershipQuery(user.id)),  # type: ignore[arg-type]
    )

    with pytest.raises(ApprovalRequiredError):
        await service_unapproved.resume_workflow(workflow_id, principal)

    assert fake_jobs.resumed_job_ids == []

    # Case 2: Approval decision is APPROVED -> calls jobs.resume()
    decision = make_test_decision(
        workflow_id=workflow_id,
        draft_id=str(uuid4()),
        status=ApprovalStatus.APPROVED,
    )
    service_approved = ClinicalWorkflowService(
        workflow_repo=repo,
        jobs=fake_jobs,  # type: ignore[arg-type]
        approvals=FakeApprovalService(decision=decision),  # type: ignore[arg-type]
        users=InMemoryUserRepo(user),
        authorization=AuthorizationService(FakeOwnershipQuery(user.id)),  # type: ignore[arg-type]
    )

    await service_approved.resume_workflow(workflow_id, principal)
    assert fake_jobs.resumed_job_ids == [job_id]
