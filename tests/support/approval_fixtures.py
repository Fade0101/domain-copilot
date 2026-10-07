"""Typed agent outputs for review tests; all approval effects use the actual gate."""

from dataclasses import replace
from uuid import UUID, uuid4

from app.application.agents.contracts import (
    ClinicalNoteDraft,
    DeferredClaim,
    SafetyClaimCheck,
    SafetyClaimStatus,
    SafetyClaimType,
    SafetyFlag,
    SafetySeverity,
    SafetyStatus,
    SafetyVerdict,
    TerminationReason,
    canonical_claim_key,
    partition_verdict_claims,
)
from app.application.approvals.contracts import DraftReview
from app.application.clinical_tools.contracts import draft_digest
from app.application.qa.grounding import REFUSAL
from app.application.retrieval.dto import Citation

ORIGINAL_NOTE = "Synthetic reviewed finding.\nFollow the documented evidence.\n"
EDITED_NOTE = "Synthetic reviewed finding.\nDocument the clinician's clarification.\n"
REJECTION_REASON = "The case requires additional supporting evidence."


def review_snapshot(workflow_id: UUID | None = None, *, kind: str = "safe") -> DraftReview:
    workflow_id = workflow_id or uuid4()
    citation = Citation(
        document_id=uuid4(),
        document_name="Synthetic review evidence",
        section="Example",
        page=1,
        chunk_id=uuid4(),
        relevance_score=0.9,
        text_snippet="Synthetic DrugA dose statement for tests only.",
    )
    claim = SafetyClaimCheck(
        SafetyClaimType.DOSAGE,
        "Synthetic DrugA",
        SafetyClaimStatus.VERIFIED_SAFE,
        "Synthetic DrugA dose statement for tests only.",
        (citation,),
    )
    safety = SafetyVerdict(
        workflow_id=workflow_id,
        status=SafetyStatus.SAFE,
        can_proceed=True,
        checked_claims=(claim,),
        flags=(),
        reasons=("Explicit test evidence",),
        citations=(citation,),
        refused=False,
        termination_reason=TerminationReason.SUFFICIENT_EVIDENCE,
        iterations=1,
        prompt_version=1,
        evidence_trace_ids=(uuid4(),),
    )
    draft = ClinicalNoteDraft(
        workflow_id=workflow_id,
        draft_id=draft_digest(ORIGINAL_NOTE),
        note=ORIGINAL_NOTE,
        asserted_claims=(claim,),
        excluded_claims=(),
        citations=(citation,),
        refused=False,
        can_proceed=True,
        evidence_trace_ids=(uuid4(),),
        metadata={"prompt_version": 1},
    )
    if kind in {"flagged", "unsupported"}:
        status = SafetyStatus.FLAGGED if kind == "flagged" else SafetyStatus.UNSUPPORTED
        concern = SafetyClaimCheck(
            SafetyClaimType.INTERACTION,
            "Synthetic DrugA, Synthetic DrugB",
            SafetyClaimStatus.FLAGGED if kind == "flagged" else SafetyClaimStatus.UNSUPPORTED,
            "Requires clinical review",
            (citation,) if kind == "flagged" else (),
        )
        flags = (
            (
                SafetyFlag(
                    "Drug interaction: Synthetic DrugA, Synthetic DrugB",
                    SafetySeverity.HIGH,
                    "Documented test concern",
                    (citation,),
                ),
            )
            if kind == "flagged"
            else ()
        )
        safety = replace(
            safety,
            status=status,
            can_proceed=False,
            refused=True,
            checked_claims=(claim, concern),
            flags=flags,
        )
        _, excluded = partition_verdict_claims(safety)
        draft = replace(draft, safety_status=status, can_proceed=False, excluded_claims=excluded)
    elif kind in {"capacity", "tool_error", "timeout", "refused"}:
        deferred: tuple[DeferredClaim, ...] = ()
        if kind == "capacity":
            deferred = (
                DeferredClaim(
                    canonical_claim_key(claim.claim_type, claim.target, claim.detail),
                    claim.detail,
                    claim.claim_type,
                    SafetyClaimStatus.VERIFIED_SAFE,
                    "Complete context exceeds tool capacity",
                    claim.citations,
                ),
            )
        elif kind in {"tool_error", "timeout"}:
            status = SafetyStatus(kind)
            safety = replace(
                safety,
                status=status,
                can_proceed=False,
                refused=True,
                termination_reason=TerminationReason(kind),
            )
        draft = replace(
            draft,
            note=REFUSAL,
            draft_id=draft_digest(REFUSAL),
            refused=True,
            asserted_claims=(),
            citations=(),
            deferred_claims=deferred,
            safety_status=safety.status,
            can_proceed=safety.can_proceed,
        )
    elif kind != "safe":
        raise ValueError("Unknown review fixture")
    return DraftReview(draft, safety)
