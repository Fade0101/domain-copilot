"""Unit tests for checkpoint restoration and idempotent workflow resume (Ticket #17)."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.agents.contracts import (
    ResearchFindings,
    TerminationReason,
)
from app.application.clinical_tools.contracts import ToolName
from app.application.ports.llm import ToolCall, ToolResult
from app.application.workflow.codecs import (
    encode_clinical_note_draft,
    encode_research_findings,
    encode_safety_verdict,
)
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.domain.approvals.entities import ApprovalStatus
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState
from tests.unit.application.test_workflow_safety_order import (
    FakeApprovalService,
    FakeClock,
    FakeJobContext,
    InMemoryUserRepo,
    InMemoryWorkflowRepo,
    make_safe_verdict,
    make_test_decision,
    make_test_draft,
    make_test_user,
)


@pytest.mark.asyncio
async def test_checkpoint_steps_are_not_rerun_on_resume() -> None:
    """When resuming from approval, research, safety, and draft steps are restored
    from checkpoints without rerun.
    """
    user = make_test_user()
    workflow_id = uuid4()
    draft = make_test_draft(workflow_id)
    draft_id = draft.draft_id
    note_id = uuid4()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    # Pre-seed existing workflow in AWAITING_APPROVAL state
    workflow = WorkflowRun(
        id=workflow_id,
        user_id=UUID(str(user.id)),
        correlation_id=str(workflow_id),
        case_summary="Case summary",
        state=ClinicalWorkflowState.AWAITING_APPROVAL,
        approval_job_id=uuid4(),
        created_at=now,
        updated_at=now,
    )
    workflow_repo = InMemoryWorkflowRepo()
    await workflow_repo.save(workflow)

    # Create dummy findings, verdict, draft
    findings = ResearchFindings(
        workflow_id=workflow_id,
        findings="Checkpointed findings",
        citations=(),
        refused=False,
        termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
        iterations=1,
        prompt_version=1,
        evidence_trace_ids=(),
    )
    verdict = make_safe_verdict(workflow_id)

    # Agents that FAIL if called
    def exploding_researcher(*a: Any, **kw: Any) -> Any:
        raise AssertionError("Researcher must NOT be called on resume from checkpoint!")

    def exploding_safety_checker(*a: Any, **kw: Any) -> Any:
        raise AssertionError("Safety Checker must NOT be called on resume from checkpoint!")

    def exploding_drafter(*a: Any, **kw: Any) -> Any:
        raise AssertionError("Drafter must NOT be called on resume from checkpoint!")

    tool_executed = False

    class FakeToolExecutor:
        async def execute(self, call: ToolCall) -> ToolResult:
            nonlocal tool_executed
            tool_executed = True
            assert call.name == ToolName.FINALIZE_CLINICAL_NOTE.value
            return ToolResult(
                tool_call_id=call.id,
                output=json.dumps({"ok": True, "result": {"note_id": str(note_id)}}),
            )

    class FakeToolFactory:
        def for_orchestrator(self, principal: Any, wid: UUID) -> FakeToolExecutor:
            return FakeToolExecutor()

    decision = make_test_decision(
        workflow_id=workflow_id,
        draft_id=draft_id,
        status=ApprovalStatus.APPROVED,
    )

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=exploding_researcher,  # type: ignore[arg-type]
        safety_checker_factory=exploding_safety_checker,  # type: ignore[arg-type]
        drafter_factory=exploding_drafter,  # type: ignore[arg-type]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=FakeApprovalService(decision=decision),  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=InMemoryUserRepo(user),
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=FakeClock(),
    )

    # Set up context WITH pre-existing checkpoints
    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "Treatment question",
            "case_summary": "Case summary",
        },
        user_id=UUID(str(user.id)),
    )
    context.checkpoints = {
        "research": {
            "findings": encode_research_findings(findings),
            "iterations": 1,
            "failed": False,
        },
        "safety_check": {"verdict": encode_safety_verdict(verdict), "iterations": 2},
        "draft": {"draft": encode_clinical_note_draft(draft), "iterations": 3},
        "approval_prepared": {"review_prepared": True, "draft_id": str(draft_id), "iterations": 3},
    }

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    # Must succeed without executing researcher, safety, or drafter!
    assert tool_executed is True
    assert result["workflow_state"] == ClinicalWorkflowState.COMPLETED.value
    assert result["note_id"] == str(note_id)

    # Check that finalize was the ONLY step executed during resume
    assert context.executed_steps == ["finalize"]
