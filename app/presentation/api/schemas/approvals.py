"""Review projections and strict commands; clients cannot submit safety/provenance."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.agents.contracts import ClinicalNoteDraft, SafetyVerdict
from app.application.approvals.contracts import (
    AWAITING_APPROVAL,
    DecisionResult,
    FinalizationRequest,
    ReviewRecord,
)
from app.application.approvals.rules import draft_is_approvable
from app.application.clinical_tools.contracts import MAX_NOTE_CHARACTERS
from app.domain.approvals.entities import ApprovalDecision, ApprovalStatus
from app.domain.jobs.entities import JobState


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)


class RejectRequest(ApproveRequest):
    reason: str = Field(min_length=3, max_length=4_000, strict=True)


class EditAndApproveRequest(ApproveRequest):
    edited_note: str = Field(min_length=1, max_length=MAX_NOTE_CHARACTERS, strict=True)


class ReviewResponse(BaseModel):
    workflow_id: UUID
    job_id: UUID
    workflow_state: str
    job_state: JobState
    cancellation_requested: bool
    approval_status: ApprovalStatus
    draft: ClinicalNoteDraft
    safety_verdict: SafetyVerdict
    approval_allowed: bool
    decision: ApprovalDecision | None
    finalization_request: FinalizationRequest | None

    @classmethod
    def from_record(cls, record: ReviewRecord) -> "ReviewResponse":
        return cls(
            workflow_id=record.workflow_id,
            job_id=record.job_id,
            workflow_state=record.workflow_state,
            job_state=record.job_state,
            cancellation_requested=record.cancellation_requested,
            approval_status=record.decision.status if record.decision else ApprovalStatus.PENDING,
            draft=record.snapshot.draft,
            safety_verdict=record.snapshot.safety_verdict,
            approval_allowed=(
                record.workflow_state == AWAITING_APPROVAL
                and record.job_state == JobState.STARTED
                and record.decision is None
                and not record.cancellation_requested
                and draft_is_approvable(record.snapshot)
            ),
            decision=record.decision,
            finalization_request=record.finalization_request,
        )


class DecisionResponse(BaseModel):
    review: ReviewResponse
    replayed: bool

    @classmethod
    def from_result(cls, result: DecisionResult) -> "DecisionResponse":
        return cls(review=ReviewResponse.from_record(result.review), replayed=result.replayed)
