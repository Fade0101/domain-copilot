"""Clinical workflow aggregate entity (Ticket #17)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from app.domain.workflow.state import (
    ClinicalWorkflowState,
    is_terminal_workflow_state,
    require_workflow_transition,
)


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: UUID
    user_id: UUID
    correlation_id: str
    case_summary: str
    created_at: datetime
    updated_at: datetime
    state: ClinicalWorkflowState = ClinicalWorkflowState.RESEARCH
    approval_job_id: UUID | None = None
    review_snapshot: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        case_summary: str,
        *,
        now: datetime,
        id: UUID | None = None,
        user_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> WorkflowRun:
        from uuid import uuid4

        wid = id or uuid4()
        uid = user_id or uuid4()
        cid = correlation_id or str(wid)
        return cls(
            id=wid,
            user_id=uid,
            correlation_id=cid,
            case_summary=case_summary,
            created_at=now,
            updated_at=now,
        )

    @property
    def terminal(self) -> bool:
        return is_terminal_workflow_state(self.state)

    def transition(
        self,
        target: ClinicalWorkflowState,
        now: datetime,
        *,
        approval_decision: str | None = None,
    ) -> WorkflowRun:
        require_workflow_transition(self.state, target, approval_decision=approval_decision)
        return replace(self, state=target, updated_at=now)

    def bind_job(self, job_id: UUID, now: datetime) -> WorkflowRun:
        if self.approval_job_id is not None and self.approval_job_id != job_id:
            raise ValueError("Workflow is already bound to a different execution job.")
        return replace(self, approval_job_id=job_id, updated_at=now)
