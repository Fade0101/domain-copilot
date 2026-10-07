"""Typed application contracts for Clinical Workflow Orchestration (Ticket #17)."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.application.retrieval.dto import Citation
from app.domain.workflow.state import ClinicalWorkflowState

CLINICAL_WORKFLOW_OPERATION = "clinical.workflow"


@dataclass(frozen=True, slots=True)
class ClinicalWorkflowInput:
    clinical_question: str
    case_summary: str
    workflow_id: UUID | None = None
    patient_context: str = ""


@dataclass(frozen=True, slots=True)
class InformationalFallbackResult:
    """Informational context emitted when guideline research fails.

    Hard invariant (BRD AC-5.5, Part 6):
    This context is strictly informational. It CANNOT emit a clinical note,
    cannot finalize a note, cannot bypass Safety Checker, and cannot advance
    the clinical workflow to FINALIZE.
    """

    context: str
    citations: tuple[Citation, ...] = ()
    refusal_reason: str = ""
    is_informational_only: bool = True


@dataclass(frozen=True, slots=True)
class ClinicalWorkflowResult:
    workflow_id: UUID
    workflow_state: ClinicalWorkflowState
    note_id: UUID | None = None
    draft_id: str | None = None
    approval_id: UUID | None = None
    refused: bool = False
    refusal_reason: str | None = None
    fallback: InformationalFallbackResult | None = None
    metadata: dict[str, object] = field(default_factory=dict)
