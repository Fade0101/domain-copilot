"""Serialization and deserialization helpers for checkpointed workflow stage outputs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.agents.contracts import (
    ClinicalNoteDraft,
    DeferredClaim,
    ExcludedClaim,
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
from app.application.retrieval.dto import Citation


def encode_citation(citation: Citation) -> dict[str, Any]:
    return {
        "chunk_id": str(citation.chunk_id),
        "document_id": str(citation.document_id),
        "document_name": citation.document_name,
        "text_snippet": citation.text_snippet,
        "relevance_score": citation.relevance_score,
        "section": citation.section,
        "page": citation.page,
    }


def decode_citation(data: dict[str, Any]) -> Citation:
    return Citation(
        chunk_id=UUID(data["chunk_id"]),
        document_id=UUID(data["document_id"]),
        document_name=data.get("document_name", ""),
        text_snippet=data["text_snippet"],
        relevance_score=float(data["relevance_score"]),
        section=data.get("section"),
        page=data.get("page"),
    )


def encode_research_findings(findings: ResearchFindings) -> dict[str, Any]:
    return {
        "workflow_id": str(findings.workflow_id),
        "findings": findings.findings,
        "citations": [encode_citation(c) for c in findings.citations],
        "refused": findings.refused,
        "termination_reason": findings.termination_reason.value,
        "iterations": findings.iterations,
        "prompt_version": findings.prompt_version,
        "evidence_trace_ids": [str(t) for t in findings.evidence_trace_ids],
        "quarantined_chunks_count": findings.quarantined_chunks_count,
        "metadata": findings.metadata,
    }


def decode_research_findings(data: dict[str, Any]) -> ResearchFindings:
    return ResearchFindings(
        workflow_id=UUID(data["workflow_id"]),
        findings=data["findings"],
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
        refused=bool(data["refused"]),
        termination_reason=TerminationReason(data["termination_reason"]),
        iterations=int(data["iterations"]),
        prompt_version=int(data["prompt_version"]),
        evidence_trace_ids=tuple(UUID(t) for t in data.get("evidence_trace_ids", ())),
        quarantined_chunks_count=int(data.get("quarantined_chunks_count", 0)),
        metadata=dict(data.get("metadata", {})),
    )


def encode_claim_check(check: SafetyClaimCheck) -> dict[str, Any]:
    return {
        "claim_type": check.claim_type.value,
        "target": check.target,
        "status": check.status.value,
        "detail": check.detail,
        "citations": [encode_citation(c) for c in check.citations],
    }


def decode_claim_check(data: dict[str, Any]) -> SafetyClaimCheck:
    return SafetyClaimCheck(
        claim_type=SafetyClaimType(data["claim_type"]),
        target=data["target"],
        status=SafetyClaimStatus(data["status"]),
        detail=data["detail"],
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
    )


def encode_flag(flag: SafetyFlag) -> dict[str, Any]:
    return {
        "claim": flag.claim,
        "severity": flag.severity.value,
        "reason": flag.reason,
        "citations": [encode_citation(c) for c in flag.citations],
        "evidence_snippet": flag.evidence_snippet,
    }


def decode_flag(data: dict[str, Any]) -> SafetyFlag:
    return SafetyFlag(
        claim=data["claim"],
        severity=SafetySeverity(data["severity"]),
        reason=data["reason"],
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
        evidence_snippet=data.get("evidence_snippet"),
    )


def encode_safety_verdict(verdict: SafetyVerdict) -> dict[str, Any]:
    return {
        "workflow_id": str(verdict.workflow_id),
        "status": verdict.status.value,
        "can_proceed": verdict.can_proceed,
        "checked_claims": [encode_claim_check(c) for c in verdict.checked_claims],
        "flags": [encode_flag(f) for f in verdict.flags],
        "reasons": list(verdict.reasons),
        "citations": [encode_citation(c) for c in verdict.citations],
        "refused": verdict.refused,
        "termination_reason": verdict.termination_reason.value,
        "iterations": verdict.iterations,
        "prompt_version": verdict.prompt_version,
        "evidence_trace_ids": [str(t) for t in verdict.evidence_trace_ids],
        "quarantined_chunks_count": verdict.quarantined_chunks_count,
        "metadata": verdict.metadata,
    }


def decode_safety_verdict(data: dict[str, Any]) -> SafetyVerdict:
    return SafetyVerdict(
        workflow_id=UUID(data["workflow_id"]),
        status=SafetyStatus(data["status"]),
        can_proceed=bool(data["can_proceed"]),
        checked_claims=tuple(decode_claim_check(c) for c in data.get("checked_claims", ())),
        flags=tuple(decode_flag(f) for f in data.get("flags", ())),
        reasons=tuple(data.get("reasons", ())),
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
        refused=bool(data["refused"]),
        termination_reason=TerminationReason(data["termination_reason"]),
        iterations=int(data["iterations"]),
        prompt_version=int(data["prompt_version"]),
        evidence_trace_ids=tuple(UUID(t) for t in data.get("evidence_trace_ids", ())),
        quarantined_chunks_count=int(data.get("quarantined_chunks_count", 0)),
        metadata=dict(data.get("metadata", {})),
    )


def encode_excluded_claim(claim: ExcludedClaim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim": claim.claim,
        "claim_type": claim.claim_type.value,
        "status": claim.status.value,
        "reason": claim.reason,
        "citations": [encode_citation(c) for c in claim.citations],
    }


def decode_excluded_claim(data: dict[str, Any]) -> ExcludedClaim:
    return ExcludedClaim(
        claim_id=data["claim_id"],
        claim=data["claim"],
        claim_type=SafetyClaimType(data["claim_type"]),
        status=SafetyClaimStatus(data["status"]),
        reason=data["reason"],
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
    )


def encode_deferred_claim(claim: DeferredClaim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim": claim.claim,
        "claim_type": claim.claim_type.value,
        "status": claim.status.value,
        "reason": claim.reason,
        "citations": [encode_citation(c) for c in claim.citations],
    }


def decode_deferred_claim(data: dict[str, Any]) -> DeferredClaim:
    return DeferredClaim(
        claim_id=data["claim_id"],
        claim=data["claim"],
        claim_type=SafetyClaimType(data["claim_type"]),
        status=SafetyClaimStatus(data["status"]),
        reason=data["reason"],
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
    )


def encode_clinical_note_draft(draft: ClinicalNoteDraft) -> dict[str, Any]:
    return {
        "workflow_id": str(draft.workflow_id),
        "draft_id": draft.draft_id,
        "note": draft.note,
        "asserted_claims": [encode_claim_check(c) for c in draft.asserted_claims],
        "excluded_claims": [encode_excluded_claim(c) for c in draft.excluded_claims],
        "deferred_claims": [encode_deferred_claim(c) for c in draft.deferred_claims],
        "citations": [encode_citation(c) for c in draft.citations],
        "refused": draft.refused,
        "safety_status": draft.safety_status.value,
        "can_proceed": draft.can_proceed,
        "requires_review": True,
        "evidence_trace_ids": [str(t) for t in draft.evidence_trace_ids],
        "quarantined_chunks_count": draft.quarantined_chunks_count,
        "metadata": draft.metadata,
    }


def decode_clinical_note_draft(data: dict[str, Any]) -> ClinicalNoteDraft:
    return ClinicalNoteDraft(
        workflow_id=UUID(data["workflow_id"]),
        draft_id=data["draft_id"],
        note=data["note"],
        asserted_claims=tuple(decode_claim_check(c) for c in data.get("asserted_claims", ())),
        excluded_claims=tuple(decode_excluded_claim(c) for c in data.get("excluded_claims", ())),
        deferred_claims=tuple(decode_deferred_claim(c) for c in data.get("deferred_claims", ())),
        citations=tuple(decode_citation(c) for c in data.get("citations", ())),
        refused=bool(data["refused"]),
        safety_status=SafetyStatus(data["safety_status"]),
        can_proceed=bool(data["can_proceed"]),
        requires_review=True,
        evidence_trace_ids=tuple(UUID(t) for t in data.get("evidence_trace_ids", ())),
        quarantined_chunks_count=int(data.get("quarantined_chunks_count", 0)),
        metadata=dict(data.get("metadata", {})),
    )
