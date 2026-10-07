"""Approval guard consumed inside the persistence adapter's locked transaction."""

from dataclasses import dataclass
from uuid import UUID

from app.application.clinical_tools.contracts import (
    MAX_NOTE_CHARACTERS,
    FinalizeClinicalNoteInput,
    draft_digest,
    require_text,
)
from app.application.clinical_tools.errors import ApprovalMismatchError
from app.domain.shared.errors import ApprovalRequiredError


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    approval_id: UUID
    workflow_id: UUID
    status: str
    original_note: str
    approved_note: str | None


def verified_approved_note(
    request: FinalizeClinicalNoteInput, approval: ApprovalSnapshot | None
) -> str:
    if approval is None:
        raise ApprovalRequiredError("Persisted approval is required")
    if approval.workflow_id != request.workflow_id or approval.approval_id != request.approval_id:
        raise ApprovalMismatchError("Approval belongs to another workflow")
    if approval.status != "APPROVED":
        raise ApprovalRequiredError("Approval status must be exactly APPROVED")
    if draft_digest(approval.original_note) != request.draft_id:
        raise ApprovalMismatchError("Approval belongs to another draft")
    if approval.approved_note is None or not approval.approved_note.strip():
        raise ApprovalRequiredError("An explicit reviewed note is required")
    require_text(approval.approved_note, "approved note", MAX_NOTE_CHARACTERS)
    return approval.approved_note
