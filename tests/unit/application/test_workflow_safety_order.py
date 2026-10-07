"""Unit tests verifying strict pipeline ordering and mandatory Safety Checker gate (Ticket #17)."""

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.agents.contracts import (
    CaseSummary,
    Citation,
    ClinicalNoteDraft,
    ResearchFindings,
    SafetyClaimCheck,
    SafetyClaimStatus,
    SafetyClaimType,
    SafetyFlag,
    SafetySeverity,
    SafetyStatus,
    SafetyVerdict,
    TerminationReason,
)
from app.application.errors import JobCancelled, JobPaused
from app.application.ports.llm import ToolCall, ToolResult
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock
from app.application.ports.workflow import IWorkflowRunRepository
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.domain.approvals.entities import ApprovalAction, ApprovalDecision, ApprovalStatus
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, Role, UserId
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState


class FakeClock(IClock):
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class InMemoryWorkflowRepo(IWorkflowRunRepository):
    def __init__(self) -> None:
        self.runs: dict[UUID, WorkflowRun] = {}

    async def get_by_id(self, workflow_id: UUID) -> WorkflowRun | None:
        return self.runs.get(workflow_id)

    async def save(self, run: WorkflowRun) -> None:
        self.runs[run.id] = run

    async def get_by_job_id(self, job_id: UUID) -> WorkflowRun | None:
        for run in self.runs.values():
            if run.approval_job_id == job_id:
                return run
        return None

    async def bind_job(self, workflow_id: UUID, job_id: UUID, now: datetime) -> None:
        if workflow_id in self.runs:
            self.runs[workflow_id] = self.runs[workflow_id].bind_job(job_id, now)

    async def update_state(
        self,
        workflow_id: UUID,
        state: ClinicalWorkflowState,
        now: datetime,
        *,
        approval_decision: str | None = None,
    ) -> None:
        if workflow_id in self.runs:
            self.runs[workflow_id] = self.runs[workflow_id].transition(
                state, now, approval_decision=approval_decision
            )


class InMemoryUserRepo(IUserRepository):
    def __init__(self, user: User) -> None:
        self.user = user

    async def get_by_id(self, user_id: UserId) -> User | None:
        return self.user if str(self.user.id) == str(user_id) else None

    async def get_by_email(self, email: EmailAddress) -> User | None:
        return self.user if self.user.email == email else None

    async def add(self, user: User) -> None:
        self.user = user


class FakeJobContext:
    def __init__(self, payload: dict[str, Any], user_id: UUID) -> None:
        self.job_id = uuid4()
        self.user_id = user_id
        self.correlation_id = uuid4()
        self.payload = payload
        self.streaming = False
        self.checkpoints: dict[str, Any] = {}
        self.executed_steps: list[str] = []
        self.is_cancelled = False

    async def check_cancelled(self) -> None:
        if self.is_cancelled:
            raise JobCancelled()

    async def emit_token(self, delta: str) -> None:
        pass

    async def step(
        self, name: str, action: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        if name in self.checkpoints:
            return deepcopy(self.checkpoints[name])
        self.executed_steps.append(name)
        result = await action()
        self.checkpoints[name] = deepcopy(result)
        return deepcopy(result)


class FakeToolExecutor:
    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            output=json.dumps({"ok": True, "result": {"note_id": str(uuid4())}}),
        )


class FakeToolFactory:
    def for_orchestrator(self, principal: Any, wid: UUID) -> FakeToolExecutor:
        return FakeToolExecutor()


class FakeApprovalService:
    def __init__(self, decision: ApprovalDecision | None = None) -> None:
        self.decision = decision

    async def prepare_review(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_decision(self, *args: Any, **kwargs: Any) -> ApprovalDecision | None:
        return self.decision


def make_test_user() -> User:
    return User(
        id=UserId(str(uuid4())),
        email=EmailAddress("reviewer1@hospital.org"),
        role=Role.REVIEWER,
        hashed_password="valid_hashed_password_digest",
        created_at=datetime.now(UTC),
    )


def make_safe_verdict(workflow_id: UUID) -> SafetyVerdict:
    return SafetyVerdict(
        workflow_id=workflow_id,
        status=SafetyStatus.SAFE,
        can_proceed=True,
        checked_claims=(),
        flags=(),
        reasons=(),
        citations=(),
        refused=False,
        termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
        iterations=1,
        prompt_version=1,
        evidence_trace_ids=(uuid4(),),
    )


def make_unsafe_verdict(workflow_id: UUID) -> SafetyVerdict:
    claim = SafetyClaimCheck(
        claim_type=SafetyClaimType.DOSAGE,
        target="Aspirin",
        status=SafetyClaimStatus.FLAGGED,
        detail="Overdose risk",
        citations=(),
    )
    flag = SafetyFlag(
        claim="Aspirin overdose",
        severity=SafetySeverity.CRITICAL,
        reason="Overdose risk",
        citations=(),
    )
    return SafetyVerdict(
        workflow_id=workflow_id,
        status=SafetyStatus.FLAGGED,
        can_proceed=False,
        checked_claims=(claim,),
        flags=(flag,),
        reasons=("Overdose risk",),
        citations=(),
        refused=True,
        termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
        iterations=1,
        prompt_version=1,
        evidence_trace_ids=(uuid4(),),
    )


def make_test_draft(
    workflow_id: UUID, note_text: str = "HPI: Chest pain. Plan: Aspirin."
) -> ClinicalNoteDraft:
    from app.application.clinical_tools.contracts import draft_digest

    dig = draft_digest(note_text)
    citation = Citation(
        document_id=uuid4(),
        document_name="Handbook",
        section="1",
        page=1,
        chunk_id=uuid4(),
        relevance_score=0.9,
        text_snippet="Aspirin 81mg indicated.",
    )
    return ClinicalNoteDraft(
        workflow_id=workflow_id,
        draft_id=dig,
        note=note_text,
        asserted_claims=(),
        excluded_claims=(),
        citations=(citation,),
        refused=False,
        safety_status=SafetyStatus.SAFE,
        can_proceed=True,
        requires_review=True,
    )


def make_test_decision(
    workflow_id: UUID,
    draft_id: str,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    reason: str | None = None,
) -> ApprovalDecision:
    action = ApprovalAction.APPROVE if status == ApprovalStatus.APPROVED else ApprovalAction.REJECT
    return ApprovalDecision(
        id=uuid4(),
        workflow_id=workflow_id,
        draft_id=draft_id,
        actor_id=uuid4(),
        actor_role=Role.REVIEWER,
        action=action,
        status=status,
        original_note="Original note text",
        approved_note="Approved note text" if status == ApprovalStatus.APPROVED else None,
        approved_draft_id=draft_id if status == ApprovalStatus.APPROVED else None,
        reason=reason or ("Approved" if status == ApprovalStatus.APPROVED else "Rejected"),
        diff=None,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_pipeline_execution_order_researcher_safety_drafter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify execution order is strictly Researcher -> Safety Checker -> Drafter."""
    call_log: list[str] = []
    user = make_test_user()
    workflow_id = uuid4()

    class FakeResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            call_log.append("researcher")
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
            call_log.append("safety_checker")
            assert "researcher" in call_log
            return make_safe_verdict(findings.workflow_id)

    class FakeDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            call_log.append("drafter")
            assert call_log == ["researcher", "safety_checker", "drafter"]
            return make_test_draft(case.workflow_id)

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: FakeResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: FakeSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: FakeDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=FakeApprovalService(),  # type: ignore[arg-type]
        workflow_repo=InMemoryWorkflowRepo(),
        user_repo=InMemoryUserRepo(user),
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=FakeClock(),
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "What is the recommended dosage?",
            "case_summary": "45yo male with hypertension.",
        },
        user_id=UUID(str(user.id)),
    )

    # Step through: must raise JobPaused at stage 4 (awaiting approval)
    with pytest.raises(JobPaused):
        await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify execution order was strictly observed
    assert call_log == ["researcher", "safety_checker", "drafter"]


@pytest.mark.asyncio
async def test_unsafe_verdict_fails_closed_and_halts_pipeline() -> None:
    """When Safety Checker finds an unsafe claim, pipeline fails closed immediately
    without calling Drafter.
    """
    call_log: list[str] = []
    user = make_test_user()
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()

    class FakeResearcher:
        async def execute(self, case: CaseSummary) -> ResearchFindings:
            call_log.append("researcher")
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
            call_log.append("safety_checker")
            return make_unsafe_verdict(findings.workflow_id)

    class FakeDrafter:
        async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
            call_log.append("drafter")
            raise AssertionError("Drafter must NOT be called when safety check fails!")

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: FakeResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: FakeSafetyChecker(),  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: FakeDrafter(),  # type: ignore[arg-type, return-value]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=FakeApprovalService(),  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=InMemoryUserRepo(user),
        fallback=InformationalRagFallback(None),  # type: ignore[arg-type]
        clock=FakeClock(),
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "What is the recommended dosage?",
            "case_summary": "45yo male with hypertension.",
        },
        user_id=UUID(str(user.id)),
    )

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify Drafter was never called
    assert call_log == ["researcher", "safety_checker"]
    assert result["refused"] is True
    assert result["workflow_state"] == ClinicalWorkflowState.FAILED.value
    assert "Overdose risk" in result["violations"]
    assert "Overdose risk" in result["reasoning"]

    # Verify workflow state in repository is FAILED
    saved = await workflow_repo.get_by_id(workflow_id)
    assert saved is not None
    assert saved.state == ClinicalWorkflowState.FAILED
