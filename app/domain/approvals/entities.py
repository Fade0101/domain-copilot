"""Immutable human decisions. No clinical inference or infrastructure dependencies."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.auth.value_objects import Role


class ApprovalAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT_AND_APPROVE = "EDIT_AND_APPROVE"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    id: UUID
    workflow_id: UUID
    draft_id: str
    actor_id: UUID
    actor_role: Role
    action: ApprovalAction
    status: ApprovalStatus
    original_note: str
    approved_note: str | None
    approved_draft_id: str | None
    reason: str | None
    diff: str | None
    created_at: datetime
