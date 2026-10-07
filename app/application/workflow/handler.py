"""Job handler for the clinical workflow operation (Ticket #17)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.application.clinical_tools.contracts import MAX_CASE_CHARACTERS, require_text
from app.application.ports.jobs import IJobContext, IJobHandler
from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS
from app.application.workflow.contracts import CLINICAL_WORKFLOW_OPERATION
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.domain.shared.errors import InvariantViolationError


class ClinicalWorkflowJobHandler(IJobHandler):
    operation_type = CLINICAL_WORKFLOW_OPERATION

    def __init__(
        self,
        orchestrator: ClinicalWorkflowOrchestrator | Callable[[], ClinicalWorkflowOrchestrator],
    ) -> None:
        self._orchestrator = orchestrator

    def _resolve_orchestrator(self) -> ClinicalWorkflowOrchestrator:
        if callable(self._orchestrator) and not isinstance(
            self._orchestrator, ClinicalWorkflowOrchestrator
        ):
            return self._orchestrator()
        return self._orchestrator

    def validate(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise InvariantViolationError("Workflow payload must be a JSON dictionary.")
        if set(payload) - {"clinical_question", "case_summary", "patient_context", "workflow_id"}:
            raise InvariantViolationError("Unknown field in clinical workflow payload.")

        question = payload.get("clinical_question")
        if not isinstance(question, str) or not question.strip():
            raise InvariantViolationError("clinical_question is required.")
        require_text(question, "clinical_question", MAX_QUERY_CHARACTERS)

        summary = payload.get("case_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise InvariantViolationError("case_summary is required.")
        require_text(summary, "case_summary", MAX_CASE_CHARACTERS)

        patient_context = payload.get("patient_context", "")
        if patient_context:
            require_text(patient_context, "patient_context", MAX_CASE_CHARACTERS, optional=True)

        raw_id = payload.get("workflow_id")
        if raw_id is not None:
            try:
                UUID(str(raw_id))
            except (ValueError, TypeError) as exc:
                raise InvariantViolationError("Invalid workflow_id UUID.") from exc

    async def run(self, context: IJobContext) -> dict[str, Any]:
        self.validate(context.payload)
        orchestrator = self._resolve_orchestrator()
        return await orchestrator.run(context)
