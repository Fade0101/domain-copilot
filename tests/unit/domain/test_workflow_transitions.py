"""Unit tests for clinical workflow state transitions (Ticket #17)."""

from datetime import UTC, datetime

import pytest

from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.errors import InvalidWorkflowTransitionError
from app.domain.workflow.state import (
    ClinicalWorkflowState,
    can_transition_workflow,
    is_terminal_workflow_state,
    require_workflow_transition,
)


def test_approved_is_not_a_workflow_state() -> None:
    """CRITICAL RULE: APPROVED must NOT be introduced as a ClinicalWorkflowState."""
    state_names = [s.name for s in ClinicalWorkflowState]
    state_values = [s.value for s in ClinicalWorkflowState]
    assert "APPROVED" not in state_names
    assert "APPROVED" not in state_values


def test_valid_sequential_transitions() -> None:
    """Happy path sequential pipeline:
    RESEARCH -> SAFETY_CHECK -> DRAFT -> AWAITING_APPROVAL -> FINALIZE -> COMPLETED.
    """
    now = datetime.now(UTC)
    workflow = WorkflowRun.create(
        case_summary="Patient presented with chest pain",
        user_id=None,
        now=now,
    )
    assert workflow.state == ClinicalWorkflowState.RESEARCH

    workflow = workflow.transition(ClinicalWorkflowState.SAFETY_CHECK, now)
    assert workflow.state == ClinicalWorkflowState.SAFETY_CHECK

    workflow = workflow.transition(ClinicalWorkflowState.DRAFT, now)
    assert workflow.state == ClinicalWorkflowState.DRAFT

    workflow = workflow.transition(ClinicalWorkflowState.AWAITING_APPROVAL, now)
    assert workflow.state == ClinicalWorkflowState.AWAITING_APPROVAL

    # Requires approved decision
    workflow = workflow.transition(
        ClinicalWorkflowState.FINALIZE, now, approval_decision="APPROVED"
    )
    assert workflow.state == ClinicalWorkflowState.FINALIZE

    workflow = workflow.transition(ClinicalWorkflowState.COMPLETED, now)
    assert workflow.state == ClinicalWorkflowState.COMPLETED
    assert is_terminal_workflow_state(workflow.state)


def test_rejection_transition() -> None:
    """AWAITING_APPROVAL -> REJECTED when human reviewer rejects."""
    now = datetime.now(UTC)
    workflow = WorkflowRun.create(
        case_summary="Patient presented with cough",
        now=now,
    )
    workflow = workflow.transition(ClinicalWorkflowState.SAFETY_CHECK, now)
    workflow = workflow.transition(ClinicalWorkflowState.DRAFT, now)
    workflow = workflow.transition(ClinicalWorkflowState.AWAITING_APPROVAL, now)

    # Rejection requires REJECTED decision
    workflow = workflow.transition(
        ClinicalWorkflowState.REJECTED, now, approval_decision="REJECTED"
    )
    assert workflow.state == ClinicalWorkflowState.REJECTED
    assert is_terminal_workflow_state(workflow.state)


def test_invalid_transitions_rejected() -> None:
    """Skipping steps or invalid transitions must raise InvalidWorkflowTransitionError."""
    now = datetime.now(UTC)
    workflow = WorkflowRun.create(case_summary="Case summary", now=now)

    # Cannot skip from RESEARCH directly to DRAFT or FINALIZE
    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.DRAFT, now)

    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.FINALIZE, now)

    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.COMPLETED, now)

    # Move to SAFETY_CHECK
    workflow = workflow.transition(ClinicalWorkflowState.SAFETY_CHECK, now)

    # Cannot skip from SAFETY_CHECK directly to FINALIZE
    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.FINALIZE, now)

    # Move to DRAFT
    workflow = workflow.transition(ClinicalWorkflowState.DRAFT, now)

    # Cannot skip from DRAFT directly to FINALIZE without human approval gate
    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.FINALIZE, now)


def test_awaiting_approval_requires_explicit_decision() -> None:
    """Transition from AWAITING_APPROVAL requires valid decision string."""
    now = datetime.now(UTC)
    workflow = WorkflowRun.create(case_summary="Case summary", now=now)
    workflow = workflow.transition(ClinicalWorkflowState.SAFETY_CHECK, now)
    workflow = workflow.transition(ClinicalWorkflowState.DRAFT, now)
    workflow = workflow.transition(ClinicalWorkflowState.AWAITING_APPROVAL, now)

    # Transitioning to FINALIZE without decision or with wrong decision raises error
    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.FINALIZE, now)

    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.FINALIZE, now, approval_decision="PENDING")

    # Transitioning to REJECTED without decision raises error
    with pytest.raises(InvalidWorkflowTransitionError):
        workflow.transition(ClinicalWorkflowState.REJECTED, now)


def test_terminal_states_cannot_transition() -> None:
    """COMPLETED, REJECTED, FAILED, and CANCELLED cannot transition anywhere."""
    for terminal in (
        ClinicalWorkflowState.COMPLETED,
        ClinicalWorkflowState.REJECTED,
        ClinicalWorkflowState.FAILED,
        ClinicalWorkflowState.CANCELLED,
    ):
        assert is_terminal_workflow_state(terminal)
        for target in ClinicalWorkflowState:
            assert not can_transition_workflow(terminal, target)
            with pytest.raises(InvalidWorkflowTransitionError):
                require_workflow_transition(terminal, target)
