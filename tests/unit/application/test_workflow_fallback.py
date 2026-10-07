"""Unit tests for informational RAG fallback behavior (Ticket #17)."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.agents.contracts import CaseSummary
from app.application.qa.use_cases import AskResult
from app.application.retrieval.dto import Citation
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.domain.workflow.state import ClinicalWorkflowState
from tests.unit.application.test_workflow_safety_order import (
    FakeClock,
    FakeJobContext,
    FakeToolFactory,
    InMemoryUserRepo,
    InMemoryWorkflowRepo,
    make_test_user,
)


@pytest.mark.asyncio
async def test_informational_rag_fallback_fails_closed() -> None:
    """When research fails, informational RAG fallback provides background context
    but CANNOT produce a draft, approve, or finalize.
    """
    user = make_test_user()
    workflow_id = uuid4()
    workflow_repo = InMemoryWorkflowRepo()

    # Researcher that fails
    class FailingResearcher:
        async def execute(self, case: CaseSummary) -> Any:
            raise RuntimeError("External guideline search service unavailable.")

    # Informational RAG AskUseCase mock
    class FakeAskUseCase:
        async def execute(self, question: str, principal: Any) -> AskResult:
            citation = Citation(
                document_id=uuid4(),
                document_name="Emergency Medicine Handbook",
                section="Section 4",
                page=12,
                chunk_id=uuid4(),
                relevance_score=0.85,
                text_snippet="Hypertensive urgency requires gradual blood pressure lowering.",
            )
            return AskResult(
                answer="Hypertensive urgency requires gradual blood pressure lowering.",
                citations=(citation,),
                refused=False,
                trace_id="test_trace",
            )

    fallback = InformationalRagFallback(FakeAskUseCase())  # type: ignore[arg-type]

    orchestrator = ClinicalWorkflowOrchestrator(
        researcher_factory=lambda p, w: FailingResearcher(),  # type: ignore[arg-type, return-value]
        safety_checker_factory=lambda p, w: None,  # type: ignore[arg-type, return-value]
        drafter_factory=lambda p, w: None,  # type: ignore[arg-type, return-value]
        tool_factory=FakeToolFactory(),  # type: ignore[arg-type]
        approval_service=None,  # type: ignore[arg-type]
        workflow_repo=workflow_repo,
        user_repo=InMemoryUserRepo(user),
        fallback=fallback,
        clock=FakeClock(),
    )

    context = FakeJobContext(
        payload={
            "workflow_id": str(workflow_id),
            "clinical_question": "What is the recommended management?",
            "case_summary": "Patient presented with severe hypertension.",
        },
        user_id=UUID(str(user.id)),
    )

    result = await orchestrator.run(context)  # type: ignore[arg-type]

    # Verify fail-closed behavior (Rule 8)
    assert result["refused"] is True
    assert result["workflow_state"] == ClinicalWorkflowState.FAILED.value
    assert "fallback" in result
    assert (
        result["fallback"]["context"]
        == "Hypertensive urgency requires gradual blood pressure lowering."
    )
    assert len(result["fallback"]["citations"]) == 1

    # Verify workflow cannot produce draft or finalize
    assert "draft_id" not in result
    assert "note_id" not in result

    # Verify workflow in repository is FAILED (NOT awaiting approval or completed)
    saved = await workflow_repo.get_by_id(workflow_id)
    assert saved is not None
    assert saved.state == ClinicalWorkflowState.FAILED
