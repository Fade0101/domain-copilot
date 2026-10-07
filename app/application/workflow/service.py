"""Application service for clinical workflows (Ticket #17)."""

from __future__ import annotations

from uuid import UUID

from app.application.approvals.service import ApprovalService
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import FinalizeClinicalNoteOutput
from app.application.errors import (
    ConfigurationError,
    ResourceNotFoundError,
    UnknownPrincipalError,
)
from app.application.jobs.service import JobService
from app.application.ports.repositories import IUserRepository
from app.application.ports.workflow import IWorkflowRunRepository
from app.domain.approvals.entities import ApprovalStatus
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.shared.errors import ApprovalRequiredError
from app.domain.workflow.entities import WorkflowRun


class ClinicalWorkflowService:
    def __init__(
        self,
        workflow_repo: IWorkflowRunRepository,
        jobs: JobService,
        approvals: ApprovalService,
        users: IUserRepository,
        authorization: AuthorizationService,
    ) -> None:
        self._workflows = workflow_repo
        self._jobs = jobs
        self._approvals = approvals
        self._users = users
        self._authorization = authorization

    async def _authorize(
        self, principal: Principal, workflow_id: UUID, permissions: tuple[Permission, ...]
    ) -> Principal:
        if not isinstance(principal, Principal):
            raise UnknownPrincipalError("An authenticated principal is required.")
        user = await self._users.get_by_id(principal.user_id)
        if user is None:
            raise UnknownPrincipalError("Workflow actor is no longer present.")
        current = Principal.from_user(user)
        for permission in permissions:
            self._authorization.require_permission(current, permission)
        await self._authorization.require_resource_access(
            current, ResourceType.RUN, str(workflow_id)
        )
        return current

    async def get_workflow(self, workflow_id: UUID, principal: Principal) -> WorkflowRun:
        await self._authorize(principal, workflow_id, (Permission.VIEW_OWN_RUNS,))
        workflow = await self._workflows.get_by_id(workflow_id)
        if workflow is None:
            raise ResourceNotFoundError(f"Workflow {workflow_id} not found.")
        return workflow

    async def get_final_note(
        self, workflow_id: UUID, principal: Principal
    ) -> FinalizeClinicalNoteOutput:
        await self._authorize(principal, workflow_id, (Permission.VIEW_OWN_RUNS,))
        note = await self._workflows.get_final_note(workflow_id)
        if note is None:
            raise ResourceNotFoundError("No finalized clinical note is available yet.")
        return note

    async def resume_workflow(self, workflow_id: UUID, principal: Principal) -> None:
        """Resume an approved workflow using the existing #20/#22 JobService.resume path."""
        await self._authorize(principal, workflow_id, (Permission.RUN_WORKFLOW,))
        workflow = await self._workflows.get_by_id(workflow_id)
        if workflow is None:
            raise ResourceNotFoundError(f"Workflow {workflow_id} not found.")
        if workflow.approval_job_id is None:
            raise ConfigurationError("Workflow has no bound execution job.")

        decision = await self._approvals.get_decision(workflow_id, principal)
        if decision is None or decision.status != ApprovalStatus.APPROVED:
            raise ApprovalRequiredError("A persisted APPROVED decision is required to resume.")

        await self._jobs.resume(workflow.approval_job_id)
