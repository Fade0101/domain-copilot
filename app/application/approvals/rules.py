"""Deterministic review policy shared by the service and its locked transaction."""

from datetime import datetime
from difflib import unified_diff
from uuid import UUID

from app.application.agents.contracts import (
    SafetyClaimStatus,
    SafetyStatus,
    TerminationReason,
)
from app.application.approvals.contracts import ApprovalCommand, DraftReview
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    MAX_NOTE_CHARACTERS,
    draft_digest,
    require_text,
)
from app.domain.approvals.entities import ApprovalAction, ApprovalDecision, ApprovalStatus
from app.domain.auth.value_objects import Permission
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError


def decision_permissions(action: ApprovalAction) -> tuple[Permission, ...]:
    if action == ApprovalAction.APPROVE:
        return (Permission.APPROVE_CLINICAL_NOTE,)
    if action == ApprovalAction.REJECT:
        return (Permission.REJECT_CLINICAL_NOTE,)
    if action == ApprovalAction.EDIT_AND_APPROVE:
        return (Permission.EDIT_CLINICAL_NOTE, Permission.APPROVE_CLINICAL_NOTE)
    raise InvariantViolationError("Unknown approval action")


def validate_command(command: ApprovalCommand) -> None:
    if command.action == ApprovalAction.REJECT:
        reason = command.reason
        if reason is None:
            raise InvariantViolationError("A meaningful rejection reason is required")
        require_text(reason, "rejection reason", 4_000)
        if len(reason.strip()) < 3 or not any(char.isalnum() for char in reason):
            raise InvariantViolationError("A meaningful rejection reason is required")
    elif command.reason is not None:
        raise InvariantViolationError("A rejection reason is only valid for rejection")
    if command.action == ApprovalAction.EDIT_AND_APPROVE:
        if command.edited_note is None:
            raise InvariantViolationError("Edited note is required")
        require_text(command.edited_note, "edited note", MAX_NOTE_CHARACTERS)
    elif command.edited_note is not None:
        raise InvariantViolationError("Edited content is only valid for edit-and-approve")


def draft_is_approvable(snapshot: DraftReview) -> bool:
    draft, safety = snapshot.draft, snapshot.safety_verdict
    # #16 can retain can_proceed=True on a capacity refusal. Both gates matter.
    return bool(
        not draft.refused
        and draft.can_proceed
        and draft.safety_status == SafetyStatus.SAFE
        and draft.asserted_claims
        and draft.citations
        and not draft.deferred_claims
        and not safety.refused
        and safety.can_proceed
        and safety.status == SafetyStatus.SAFE
        and safety.termination_reason == TerminationReason.SUFFICIENT_EVIDENCE
        and safety.checked_claims
        and safety.citations
        and not safety.flags
        and all(
            claim.status == SafetyClaimStatus.VERIFIED_SAFE and claim.citations
            for claim in safety.checked_claims
        )
    )


def textual_diff(original: str, approved: str) -> str:
    """Unified diff, including final-newline/line-ending changes, with stable labels.

    Keep the terminators when splitting. A missing final newline is marked like
    a conventional patch, so edits invisible to splitlines() are still visible.
    Original and approved bytes/digests remain the primary audit evidence.
    """
    lines = unified_diff(
        original.splitlines(keepends=True),
        approved.splitlines(keepends=True),
        fromfile="original",
        tofile="approved",
        lineterm="\n",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def make_decision(
    command: ApprovalCommand,
    snapshot: DraftReview,
    actor: Principal,
    identifier: UUID,
    now: datetime,
) -> ApprovalDecision:
    validate_command(command)
    draft = snapshot.draft
    if command.workflow_id != draft.workflow_id or command.draft_id != draft.draft_id:
        raise InvalidStateTransitionError("The decision does not match the persisted draft")
    approved_note = None
    difference = None
    if command.action != ApprovalAction.REJECT:
        if not draft_is_approvable(snapshot):
            raise InvalidStateTransitionError("This draft is review-only and cannot be approved")
        approved_note = draft.note
        if command.action == ApprovalAction.EDIT_AND_APPROVE:
            approved_note = command.edited_note
            if approved_note == draft.note:
                raise InvariantViolationError("Edit-and-approve requires a change to the note")
        assert approved_note is not None
        difference = textual_diff(draft.note, approved_note)
    return ApprovalDecision(
        id=identifier,
        workflow_id=draft.workflow_id,
        draft_id=draft.draft_id,
        actor_id=UUID(actor.user_id.value),
        actor_role=actor.role,
        action=command.action,
        status=ApprovalStatus.REJECTED
        if command.action == ApprovalAction.REJECT
        else ApprovalStatus.APPROVED,
        original_note=draft.note,
        approved_note=approved_note,
        approved_draft_id=draft_digest(approved_note) if approved_note is not None else None,
        reason=command.reason if command.action == ApprovalAction.REJECT else None,
        diff=difference,
        created_at=now,
    )


def is_same_decision(
    decision: ApprovalDecision, command: ApprovalCommand, actor: Principal
) -> bool:
    """Only the same actor's exact request is an idempotent replay."""
    expected_note = (
        command.edited_note
        if command.action == ApprovalAction.EDIT_AND_APPROVE
        else decision.original_note
        if command.action == ApprovalAction.APPROVE
        else None
    )
    return (
        decision.actor_id == UUID(actor.user_id.value)
        and decision.workflow_id == command.workflow_id
        and decision.draft_id == command.draft_id
        and decision.action == command.action
        and decision.reason == command.reason
        and decision.approved_note == expected_note
    )
