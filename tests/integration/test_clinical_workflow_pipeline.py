"""Integration tests for the complete clinical workflow pipeline (Ticket #17).

Tests the end-to-end sequential workflow:
Researcher (#14) -> Safety Checker (#15) -> Drafter (#16) ->
Human Approval Gate (#19) -> Guarded Finalizer (#18)
Verifying pause/resume semantics (#20, #22), checkpoint idempotency,
fail-closed safety, and rejection flows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
from app.application.approvals.contracts import (
    DecisionResult,
    DraftReview,
    FinalizationRequest,
    ReviewRecord,
)
from app.application.approvals.service import ApprovalService
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import ToolName, draft_digest
from app.application.errors import JobPaused
from app.application.ports.approvals import IApprovalStore
from app.application.ports.audit import AuditEntry, IAuditSink
from app.application.ports.llm import ToolCall, ToolResult
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.application.workflow.service import ClinicalWorkflowService
from app.domain.approvals.entities import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalStatus,
)
from app.domain.auth.value_objects import Role, UserId
from app.domain.jobs.entities import JobState
from app.domain.workflow.state import ClinicalWorkflowState
from tests.unit.application.test_workflow_safety_order import (
    FakeClock,
    FakeJobContext,
    InMemoryUserRepo,
    InMemoryWorkflowRepo,
    make_safe_verdict,
    make_test_draft,
    make_test_user,
    make_unsafe_verdict,
)


class InMemoryApprovalStore(IApprovalStore):
    def __init__(self) -> None:
        self.reviews: dict[UUID, ReviewRecord] = {}

    async def prepare_review(
        self, snapshot: DraftReview, job_id: UUID, actor_id: UserId
    ) -> ReviewRecord:
        record = ReviewRecord(
            workflow_id=snapshot.draft.workflow_id,
            job_id=job_id,
            workflow_state="AWAITING_APPROVAL",
            job_state=JobState.STARTED,
            snapshot=snapshot,
            decision=None,
            finalization_request=None,
        )
        self.reviews[snapshot.draft.workflow_id] = record
        return record

    async def get(self, workflow_id: UUID, actor_id: UserId) -> ReviewRecord:
        if workflow_id not in self.reviews:
            raise KeyError(f"No review for workflow {workflow_id}")
        return self.reviews[workflow_id]

    async def decide(self, command: Any, actor_id: UserId) -> DecisionResult:
        record = self.reviews[command.workflow_id]
        decision_id = uuid4()
        if command.action == ApprovalAction.APPROVE:
            status = ApprovalStatus.APPROVED
            fin_req = FinalizationRequest(
                event_id=uuid4(),
                workflow_id=command.workflow_id,
                job_id=record.job_id,
                approval_id=decision_id,
                draft_id=command.draft_id,
                created_at=datetime.now(UTC),
            )
        elif command.action == ApprovalAction.REJECT:
            status = ApprovalStatus.REJECTED
            fin_req = None
        else:
            status = ApprovalStatus.APPROVED
            fin_req = FinalizationRequest(
                event_id=uuid4(),
                workflow_id=command.workflow_id,
                job_id=record.job_id,
                approval_id=decision_id,
                draft_id=draft_digest(command.edited_note or ""),
                created_at=datetime.now(UTC),
            )

        decision = ApprovalDecision(
            id=decision_id,
            workflow_id=command.workflow_id,
            draft_id=command.draft_id,
            actor_id=UUID(actor_id.value),
            actor_role=Role.REVIEWER,
            action=command.action,
            status=status,
            original_note=record.snapshot.draft.note,
            approved_note=record.snapshot.draft.note if status == ApprovalStatus.APPROVED else None,
            approved_draft_id=fin_req.draft_id if fin_req else None,
            reason=command.reason,
            diff=None,
            created_at=datetime.now(UTC),
        )
        updated_record = ReviewRecord(
            workflow_id=record.workflow_id,
            job_id=record.job_id,
            workflow_state="AWAITING_APPROVAL",
            job_state=record.job_state,
            snapshot=record.snapshot,
            decision=decision,
            finalization_request=fin_req,
        )
        self.reviews[command.workflow_id] = updated_record
        return DecisionResult(review=updated_record, replayed=False)


class NoopAuditSink(IAuditSink):
    async def record(self, entry: AuditEntry) -> None:
        pass


class FakeOwnershipQuery:
    def __init__(self, owner_id: UserId) -> None:
        self.owner_id = owner_id

    async def owner_of(self, resource_type: Any, resource_id: str) -> UserId | None:
        return self.owner_id


class FakeJobService:
    def __init__(self) -> None:
        self.resumed_job_ids: list[UUID] = []

    async def resume(self, job_id: UUID) -> None:
        self.resumed_job_ids.append(job_id)


@pytest.mark.asyncio
async def test_end_to_end_clinical_workflow_happy_path() -> None:
    """Full pipeline execution from research to safe check to draft to approval pause
    to finalization.
    """
    user = make_test_user()
    principal = Principal.from_user(user)
    workflow_id = uuid4()
    note_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()
    user_repo = InMemoryUserRepo(user)
    approval_store = InMemoryApprovalStore()
    clock = FakeClock()
    auth_service = AuthorizationService(FakeOwnershipQuery(user.id))

    approval_service = ApprovalService(
        store=approval_store,
        users=user_repo,
        authorization=auth_service,
        audit=NoopAuditSink(),
        clock=clock,
    )

    class RealMockResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            return ResearchFindings(
                workflow_id=case.workflow_id,
                findings="Evidence-based hypertension protocol: initiate lisinopril 10mg daily.",
                citations=(),
                refused=False,
                termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
                iterations=1,
                prompt_version=1,
                evidence_trace_ids=(uuid4(),),
            )

    class RealMockSafetyChecker:
        async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
            return make_safe_verdict(findings.workflow_id)

    class RealMockDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            note = (
                "Assessment and Plan: Essential hypertension. Start lisinopril 10mg po daily."
            )
            return make_test_draft(case.workflow_id, note_text=note)

    tool_executed = False

    class FinalizeToolExecutor:
        async def execute(self, call: ToolCall) -> ToolResult:
            nonlocal tool_executed
            tool_executed = True
            assert call.name == ToolName.FINALIZE_CLINICAL_NOTE.value
            args = json.loads(call.arguments)
            assert args["workflow_id"] == str(workflow_id)
            return ToolResult(
                tool_call_id=call.id,
                output=json.dumps({"ok": True, "result": {"note_id": str(note_id)}}),
            )

    class ToolFactory:
        def for_orchestrator(self, principal: Any, wid: UUID) -> FinalizeToolExecutor:
            return FinalizeToolExecutor()

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: RealMockResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: RealMockSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: RealMockDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=ToolFactory(),  # type: ignore[arg-type]
        approval_service=approval_service,
        workflow_repo=workflow_repo,
        user_repo=user_repo,
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=clock,
    )

    job_service = FakeJobService()
    workflow_service = ClinicalWorkflowService(
        workflow_repo=workflow_repo,
        jobs=job_service,  # type: ignore[arg-type]
        approvals=approval_service,
        users=user_repo,
        authorization=auth_service,
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "What is the primary pharmacotherapy for stage 1 hypertension?",
            "case_summary": "48yo female with newly diagnosed stage 1 HTN.",
        },
        user_id=UUID(str(user.id)),
    )

    # PHASE 1: Initial run -> Pauses at approval gate
    with pytest.raises(JobPaused):
        await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify workflow state in repository is AWAITING_APPROVAL
    wf = await workflow_repo.get_by_id(workflow_id)
    assert wf is not None
    assert wf.state == ClinicalWorkflowState.AWAITING_APPROVAL
    assert tool_executed is False

    # Check that draft review is available in approval store
    review = await approval_service.get_review(workflow_id, principal)
    assert review is not None
    assert review.decision is None
    draft_id = review.snapshot.draft.draft_id

    # PHASE 2: Reviewer reviews and approves the draft
    await approval_service.approve(workflow_id, draft_id, principal)

    # Resume the workflow via ClinicalWorkflowService
    await workflow_service.resume_workflow(workflow_id, principal)
    assert job_service.resumed_job_ids == [context.job_id]

    # PHASE 3: Resumed orchestrator run
    result = await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify finalizer tool was executed and workflow completed
    assert tool_executed is True
    assert result["workflow_state"] == ClinicalWorkflowState.COMPLETED.value
    assert result["note_id"] == str(note_id)

    # Verify persisted state is COMPLETED
    wf_completed = await workflow_repo.get_by_id(workflow_id)
    assert wf_completed is not None
    assert wf_completed.state == ClinicalWorkflowState.COMPLETED


@pytest.mark.asyncio
async def test_end_to_end_clinical_workflow_rejection_path() -> None:
    """When human review rejects the draft, workflow moves to REJECTED and finalizer
    is not called.
    """
    user = make_test_user()
    principal = Principal.from_user(user)
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()
    user_repo = InMemoryUserRepo(user)
    approval_store = InMemoryApprovalStore()
    clock = FakeClock()
    auth_service = AuthorizationService(FakeOwnershipQuery(user.id))

    approval_service = ApprovalService(
        store=approval_store,
        users=user_repo,
        authorization=auth_service,
        audit=NoopAuditSink(),
        clock=clock,
    )

    class RealMockResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            return ResearchFindings(
                workflow_id=case.workflow_id,
                findings="Evidence findings",
                citations=(),
                refused=False,
                termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
                iterations=1,
                prompt_version=1,
                evidence_trace_ids=(uuid4(),),
            )

    class RealMockSafetyChecker:
        async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
            return make_safe_verdict(findings.workflow_id)

    class RealMockDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            return make_test_draft(case.workflow_id, note_text="Initial draft")

    tool_executed = False

    class FinalizeToolExecutor:
        async def execute(self, call: ToolCall) -> ToolResult:
            nonlocal tool_executed
            tool_executed = True
            raise AssertionError("Finalize tool must NEVER be called upon rejection!")

    class ToolFactory:
        def for_orchestrator(self, principal: Any, wid: UUID) -> FinalizeToolExecutor:
            return FinalizeToolExecutor()

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: RealMockResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: RealMockSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: RealMockDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=ToolFactory(),  # type: ignore[arg-type]
        approval_service=approval_service,
        workflow_repo=workflow_repo,
        user_repo=user_repo,
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=clock,
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "Treatment question",
            "case_summary": "Case summary",
        },
        user_id=UUID(str(user.id)),
    )

    # Initial run -> Pauses at approval gate
    with pytest.raises(JobPaused):
        await orchestrator.run(context)  # type: ignore[arg-type]

    review = await approval_service.get_review(workflow_id, principal)
    draft_id = review.snapshot.draft.draft_id

    # Reviewer rejects the draft
    await approval_service.reject(
        workflow_id, draft_id, "Inadequate diagnostic rationale.", principal
    )

    # Resumed orchestrator run
    result = await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify result is terminal REJECTED and tool was not invoked
    assert tool_executed is False
    assert result["refused"] is True
    assert result["workflow_state"] == ClinicalWorkflowState.REJECTED.value
    assert result["rejection_reason"] == "Inadequate diagnostic rationale."

    wf = await workflow_repo.get_by_id(workflow_id)
    assert wf is not None
    assert wf.state == ClinicalWorkflowState.REJECTED


@pytest.mark.asyncio
async def test_end_to_end_safety_violation_halts_pipeline() -> None:
    """Safety Checker finding an unsafe condition immediately halts before Drafter
    or Approval gate.
    """
    user = make_test_user()
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()
    user_repo = InMemoryUserRepo(user)
    clock = FakeClock()

    class Researcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            return ResearchFindings(
                workflow_id=case.workflow_id,
                findings="Dangerous dosage proposed.",
                citations=(),
                refused=False,
                termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
                iterations=1,
                prompt_version=1,
                evidence_trace_ids=(uuid4(),),
            )

    class SafetyChecker:
        async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
            return make_unsafe_verdict(findings.workflow_id)

    class ExplodingDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            raise AssertionError("Drafter must NOT be reached when safety check fails!")

    class ToolFactory:
        def for_orchestrator(self, principal: Any, wid: UUID) -> Any:
            raise AssertionError("Tool executor must NOT be reached!")

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: Researcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: SafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: ExplodingDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=ToolFactory(),  # type: ignore[arg-type]
        approval_service=None,  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=user_repo,
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=clock,
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "Unsafe dosage check",
            "case_summary": "Case summary",
        },
        user_id=UUID(str(user.id)),
    )

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    assert result["refused"] is True
    assert result["workflow_state"] == ClinicalWorkflowState.FAILED.value
    assert "Overdose risk" in result["violations"]

    wf = await workflow_repo.get_by_id(workflow_id)
    assert wf is not None
    assert wf.state == ClinicalWorkflowState.FAILED
