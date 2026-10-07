"""Unit tests for approval gate pause, rejection, and guarded finalization (Ticket #17)."""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.agents.contracts import (
    CaseSummary,
    ClinicalNoteDraft,
    ResearchFindings,
    SafetyVerdict,
    TerminationReason,
)
from app.application.clinical_tools.contracts import ToolName
from app.application.ports.llm import ToolCall, ToolResult
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.domain.approvals.entities import ApprovalStatus
from app.domain.workflow.state import ClinicalWorkflowState
from tests.unit.application.test_workflow_safety_order import (
    FakeApprovalService,
    FakeClock,
    FakeJobContext,
    FakeToolFactory,
    InMemoryUserRepo,
    InMemoryWorkflowRepo,
    make_safe_verdict,
    make_test_decision,
    make_test_draft,
    make_test_user,
)


@pytest.mark.asyncio
async def test_rejection_marks_workflow_rejected() -> None:
    """When human review rejects the draft, workflow moves to REJECTED and returns refused."""
    user = make_test_user()
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()

    draft = make_test_draft(workflow_id)

    class FakeResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            return ResearchFindings(
                workflow_id=case.workflow_id,
                findings="Guideline findings for: " + case.clinical_question,
                citations=(),
                refused=False,
                termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
                iterations=1,
                prompt_version=1,
                evidence_trace_ids=(),
            )

    class FakeSafetyChecker:
        async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
            return make_safe_verdict(findings.workflow_id)

    class FakeDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            return draft

    decision = make_test_decision(
        workflow_id=workflow_id,
        draft_id=draft.draft_id,
        status=ApprovalStatus.REJECTED,
        reason="Inaccurate clinical statement in plan.",
    )

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: FakeResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: FakeSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: FakeDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=FakeApprovalService(decision=decision),  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=InMemoryUserRepo(user),
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=FakeClock(),
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "Treatment question",
            "case_summary": "Case summary",
        },
        user_id=UUID(str(user.id)),
    )

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    assert result["refused"] is True
    assert result["workflow_state"] == ClinicalWorkflowState.REJECTED.value
    assert result["rejection_reason"] == "Inaccurate clinical statement in plan."

    saved = await workflow_repo.get_by_id(workflow_id)
    assert saved is not None
    assert saved.state == ClinicalWorkflowState.REJECTED


@pytest.mark.asyncio
async def test_approval_executes_finalizer_and_completes_workflow() -> None:
    """When human review approves the draft, workflow moves to FINALIZE, invokes finalize tool,
    and completes.
    """
    user = make_test_user()
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()
    draft = make_test_draft(workflow_id)
    note_id = uuid4()

    class FakeResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            return ResearchFindings(
                workflow_id=case.workflow_id,
                findings="Guideline findings for: " + case.clinical_question,
                citations=(),
                refused=False,
                termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
                iterations=1,
                prompt_version=1,
                evidence_trace_ids=(),
            )

    class FakeSafetyChecker:
        async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
            return make_safe_verdict(findings.workflow_id)

    class FakeDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            return draft

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
        draft_id=draft.draft_id,
        status=ApprovalStatus.APPROVED,
    )

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: FakeResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: FakeSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: FakeDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=FakeApprovalService(decision=decision),  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=InMemoryUserRepo(user),
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=FakeClock(),
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "Treatment question",
            "case_summary": "Case summary",
        },
        user_id=UUID(str(user.id)),
    )

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    assert tool_executed is True
    assert result["workflow_state"] == ClinicalWorkflowState.COMPLETED.value
    assert result["note_id"] == str(note_id)

    saved = await workflow_repo.get_by_id(workflow_id)
    assert saved is not None
    assert saved.state == ClinicalWorkflowState.COMPLETED
