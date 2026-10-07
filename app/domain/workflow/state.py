"""Clinical workflow states and transition rules (Ticket #17).

Independent from the asynchronous Job lifecycle (BRD FR-5, T7-09).
APPROVED is an approval decision outcome (#19), not a clinical workflow phase.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.workflow.errors import InvalidWorkflowTransitionError


class ClinicalWorkflowState(StrEnum):
    RESEARCH = "RESEARCH"
    SAFETY_CHECK = "SAFETY_CHECK"
    DRAFT = "DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FINALIZE = "FINALIZE"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TRANSITIONS: dict[ClinicalWorkflowState, frozenset[ClinicalWorkflowState]] = {
    ClinicalWorkflowState.RESEARCH: frozenset(
        {
            ClinicalWorkflowState.SAFETY_CHECK,
            ClinicalWorkflowState.FAILED,
            ClinicalWorkflowState.CANCELLED,
        }
    ),
    ClinicalWorkflowState.SAFETY_CHECK: frozenset(
        {
            ClinicalWorkflowState.DRAFT,
            ClinicalWorkflowState.FAILED,
            ClinicalWorkflowState.CANCELLED,
        }
    ),
    ClinicalWorkflowState.DRAFT: frozenset(
        {
            ClinicalWorkflowState.AWAITING_APPROVAL,
            ClinicalWorkflowState.FAILED,
            ClinicalWorkflowState.CANCELLED,
        }
    ),
    ClinicalWorkflowState.AWAITING_APPROVAL: frozenset(
        {
            ClinicalWorkflowState.FINALIZE,
            ClinicalWorkflowState.REJECTED,
            ClinicalWorkflowState.CANCELLED,
        }
    ),
    ClinicalWorkflowState.FINALIZE: frozenset(
        {
            ClinicalWorkflowState.COMPLETED,
            ClinicalWorkflowState.FAILED,
            ClinicalWorkflowState.CANCELLED,
        }
    ),
    ClinicalWorkflowState.COMPLETED: frozenset(),
    ClinicalWorkflowState.REJECTED: frozenset(),
    ClinicalWorkflowState.FAILED: frozenset(),
    ClinicalWorkflowState.CANCELLED: frozenset(),
}


def is_terminal_workflow_state(state: ClinicalWorkflowState) -> bool:
    return not _TRANSITIONS[state]


def can_transition_workflow(
    current: ClinicalWorkflowState,
    target: ClinicalWorkflowState,
    *,
    approval_decision: str | None = None,
) -> bool:
    if target not in _TRANSITIONS.get(current, frozenset()):
        return False
    if current == ClinicalWorkflowState.AWAITING_APPROVAL:
        if target == ClinicalWorkflowState.FINALIZE and approval_decision != "APPROVED":
            return False
        if target == ClinicalWorkflowState.REJECTED and approval_decision != "REJECTED":
            return False
    return True


def require_workflow_transition(
    current: ClinicalWorkflowState,
    target: ClinicalWorkflowState,
    *,
    approval_decision: str | None = None,
) -> None:
    if not can_transition_workflow(current, target, approval_decision=approval_decision):
        if (
            current == ClinicalWorkflowState.AWAITING_APPROVAL
            and target == ClinicalWorkflowState.FINALIZE
        ):
            raise InvalidWorkflowTransitionError(
                "Transition from AWAITING_APPROVAL to FINALIZE requires "
                "persisted APPROVED decision."
            )
        if (
            current == ClinicalWorkflowState.AWAITING_APPROVAL
            and target == ClinicalWorkflowState.REJECTED
        ):
            raise InvalidWorkflowTransitionError(
                "Transition from AWAITING_APPROVAL to REJECTED requires "
                "persisted REJECTED decision."
            )
        raise InvalidWorkflowTransitionError(
            f"Cannot move clinical workflow from {current.value} to {target.value}."
        )
