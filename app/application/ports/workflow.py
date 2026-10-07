"""Port for durable clinical workflow runs (Ticket #17)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.clinical_tools.contracts import FinalizeClinicalNoteOutput
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState


class IWorkflowRunRepository(Protocol):
    async def get_final_note(self, workflow_id: UUID) -> FinalizeClinicalNoteOutput | None:
        """Read only the persisted #18 final note; never substitute a draft/approval."""
        ...

    async def get_by_id(self, workflow_id: UUID) -> WorkflowRun | None:
        """Return the workflow run by its primary key, or None if not found."""
        ...

    async def get_by_job_id(self, job_id: UUID) -> WorkflowRun | None:
        """Return the workflow run bound to the given execution job ID, or None."""
        ...

    async def save(self, run: WorkflowRun) -> None:
        """Insert or update a workflow run."""
        ...

    async def update_state(
        self,
        workflow_id: UUID,
        state: ClinicalWorkflowState,
        now: datetime,
        *,
        approval_decision: str | None = None,
    ) -> None:
        """Atomically persist a clinical workflow state transition."""
        ...

    async def bind_job(self, workflow_id: UUID, job_id: UUID, now: datetime) -> None:
        """Atomically bind an execution job to a workflow run before review."""
        ...
