"""Approval handoff contracts; agent results are preserved, never reconstructed."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.application.agents.contracts import ClinicalNoteDraft, SafetyVerdict
from app.application.clinical_tools.contracts import require_digest, require_uuid
from app.domain.approvals.entities import ApprovalAction, ApprovalDecision
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvariantViolationError

AWAITING_APPROVAL = "AWAITING_APPROVAL"
REVIEW_REQUESTED = "approval.review_requested"
DECISION_RECORDED = "approval.decision_recorded"
FINALIZATION_REQUESTED = "approval.finalization_requested"


@dataclass(frozen=True, slots=True)
class DraftReview:
    """Trusted #16 output plus its authoritative #15 input, persisted unchanged.

    This is an internal registration contract, never an HTTP request body.
    Refused and partial drafts remain reviewable, but cannot be approved.
    """

    draft: ClinicalNoteDraft
    safety_verdict: SafetyVerdict
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if not isinstance(self.draft, ClinicalNoteDraft) or not isinstance(
            self.safety_verdict, SafetyVerdict
        ):
            raise InvariantViolationError("Review requires the typed draft and safety verdict")
        if self.schema_version != 1:
            raise InvariantViolationError("Unsupported review snapshot version")
        if self.draft.workflow_id != self.safety_verdict.workflow_id:
            raise InvariantViolationError("Draft and safety verdict belong to different workflows")
        if self.draft.safety_status != self.safety_verdict.status:
            raise InvariantViolationError("Draft and safety verdict statuses do not match")
        if self.draft.can_proceed and not self.safety_verdict.can_proceed:
            raise InvariantViolationError("Draft cannot override its safety verdict")
        if any(
            claim not in self.safety_verdict.checked_claims for claim in self.draft.asserted_claims
        ):
            raise InvariantViolationError("Draft assertions must preserve safety claim provenance")


@dataclass(frozen=True, slots=True)
class ApprovalCommand:
    workflow_id: UUID
    draft_id: str
    action: ApprovalAction
    reason: str | None = None
    edited_note: str | None = None

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_digest(self.draft_id)
        if not isinstance(self.action, ApprovalAction):
            raise InvariantViolationError("Unknown approval action")


@dataclass(frozen=True, slots=True)
class FinalizationRequest:
    """Committed durable signal, not a broker message or authority to skip #18.

    #17 may use these IDs with JobService.resume and the guarded clinical tool.
    It must still verify its safety checkpoint, limits and workflow state.
    """

    event_id: UUID
    workflow_id: UUID
    job_id: UUID
    approval_id: UUID
    draft_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    workflow_id: UUID
    job_id: UUID
    workflow_state: str
    job_state: JobState
    snapshot: DraftReview
    decision: ApprovalDecision | None = None
    finalization_request: FinalizationRequest | None = None
    cancellation_requested: bool = False


@dataclass(frozen=True, slots=True)
class DecisionResult:
    review: ReviewRecord
    replayed: bool
