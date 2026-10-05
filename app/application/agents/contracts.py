"""Typed contracts for the Guideline Researcher and pipeline agents (AGT-01).

Pure standard-library dataclasses and enums. Clean Architecture boundary:
no framework, ORM, SDK or third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID

from app.application.clinical_tools.contracts import (
    MAX_CASE_CHARACTERS,
    draft_digest,
    require_digest,
    require_text,
    require_uuid,
)
from app.application.retrieval.dto import Citation
from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS


class TerminationReason(StrEnum):
    """Deterministic termination reasons for pipeline agent execution."""

    SUFFICIENT_EVIDENCE = "sufficient_evidence"
    EMPTY_EVIDENCE = "empty_evidence"
    LOW_EVIDENCE = "low_evidence"
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"


@dataclass(frozen=True, slots=True)
class CaseSummary:
    """Typed input contract representing a clinical case and research question."""

    workflow_id: UUID
    clinical_question: str
    case_summary: str
    patient_context: str = ""

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_text(self.clinical_question, "clinical_question", MAX_QUERY_CHARACTERS)
        require_text(self.case_summary, "case_summary", MAX_CASE_CHARACTERS)
        if self.patient_context:
            require_text(
                self.patient_context, "patient_context", MAX_CASE_CHARACTERS, optional=True
            )


@dataclass(frozen=True, slots=True)
class ResearchFindings:
    """Typed, citation-bearing output contract produced by Guideline Researcher."""

    workflow_id: UUID
    findings: str
    citations: tuple[Citation, ...]
    refused: bool
    termination_reason: TerminationReason
    iterations: int
    prompt_version: int
    evidence_trace_ids: tuple[UUID, ...]
    quarantined_chunks_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_text(self.findings, "findings", 64_000)
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")
        if not isinstance(self.termination_reason, TerminationReason):
            raise TypeError("Invalid termination_reason")
        if type(self.refused) is not bool:
            raise TypeError("refused must be a boolean")
        if type(self.iterations) is not int or self.iterations < 0:
            raise ValueError("iterations must be a non-negative integer")
        if type(self.prompt_version) is not int or self.prompt_version < 1:
            raise ValueError("prompt_version must be a positive integer")
        if not isinstance(self.evidence_trace_ids, tuple):
            raise TypeError("evidence_trace_ids must be a tuple")


class SafetyStatus(StrEnum):
    """Authoritative safety status for a workflow run."""

    SAFE = "safe"  # All claims verified safe by explicit evidence
    FLAGGED = "flagged"  # Confirmed unsafe: drug interaction or unsafe dosage
    UNSUPPORTED = "unsupported"  # Missing or unverified clinical evidence
    TOOL_ERROR = "tool_error"  # Safety tool execution failed
    TIMEOUT = "timeout"  # Safety evaluation timed out


class SafetySeverity(StrEnum):
    """Severity levels for safety flags."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"


class SafetyClaimType(StrEnum):
    """Clinical claim category."""

    DOSAGE = "dosage"
    INTERACTION = "interaction"


class SafetyClaimStatus(StrEnum):
    """Verification status for an individual claim."""

    VERIFIED_SAFE = "verified_safe"  # Explicit positive safety evidence
    UNSUPPORTED = "unsupported"  # Missing or insufficient evidence
    FLAGGED = "flagged"  # Confirmed contraindication / unsafe


@dataclass(frozen=True, slots=True)
class SafetyFlag:
    """An explicit safety violation or concern with direct evidence provenance."""

    claim: str
    severity: SafetySeverity
    reason: str
    citations: tuple[Citation, ...] = ()
    evidence_snippet: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, SafetySeverity):
            raise TypeError("severity must be a SafetySeverity")
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")
        require_text(self.claim, "claim", 4_000)
        require_text(self.reason, "reason", 4_000)


@dataclass(frozen=True, slots=True)
class SafetyClaimCheck:
    """Individual verification record for a specific clinical claim."""

    claim_type: SafetyClaimType
    target: str  # Drug name or drug pair
    status: SafetyClaimStatus
    detail: str
    citations: tuple[Citation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.claim_type, SafetyClaimType):
            raise TypeError("claim_type must be a SafetyClaimType")
        if not isinstance(self.status, SafetyClaimStatus):
            raise TypeError("status must be a SafetyClaimStatus")
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")
        require_text(self.target, "target", 1_000)
        require_text(self.detail, "detail", 4_000)


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """Typed, citation-bearing output of the Safety Checker (AGT-02)."""

    workflow_id: UUID
    status: SafetyStatus
    can_proceed: bool
    checked_claims: tuple[SafetyClaimCheck, ...]
    flags: tuple[SafetyFlag, ...]
    reasons: tuple[str, ...]
    citations: tuple[Citation, ...]  # Tool-derived, non-quarantined ONLY
    refused: bool
    termination_reason: TerminationReason
    iterations: int
    prompt_version: int
    evidence_trace_ids: tuple[UUID, ...]
    quarantined_chunks_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        if not isinstance(self.status, SafetyStatus):
            raise TypeError("status must be a SafetyStatus")
        if type(self.can_proceed) is not bool:
            raise TypeError("can_proceed must be a boolean")
        if type(self.refused) is not bool:
            raise TypeError("refused must be a boolean")
        # Invariant: can_proceed is True if and only if status is SAFE
        if self.can_proceed != (self.status == SafetyStatus.SAFE):
            raise ValueError("can_proceed must be True if and only if status is SAFE")
        # Invariant: refused is True if and only if status is not SAFE
        if self.refused != (self.status != SafetyStatus.SAFE):
            raise ValueError("refused must be True if and only if status is not SAFE")
        if not isinstance(self.checked_claims, tuple):
            raise TypeError("checked_claims must be a tuple")
        if not isinstance(self.flags, tuple):
            raise TypeError("flags must be a tuple")
        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")
        if not isinstance(self.termination_reason, TerminationReason):
            raise TypeError("Invalid termination_reason")
        if type(self.iterations) is not int or self.iterations < 0:
            raise ValueError("iterations must be a non-negative integer")
        if type(self.prompt_version) is not int or self.prompt_version < 1:
            raise ValueError("prompt_version must be a positive integer")
        if not isinstance(self.evidence_trace_ids, tuple):
            raise TypeError("evidence_trace_ids must be a tuple")
        if type(self.quarantined_chunks_count) is not int or self.quarantined_chunks_count < 0:
            raise ValueError("quarantined_chunks_count must be a non-negative integer")


def canonical_claim_key(
    claim_type: SafetyClaimType | str,
    target: str,
    detail: str = "",
) -> str:
    """Deterministic, stable key distinguishing clinical claims.

    - For DOSAGE: includes target drug and normalized detail text to differentiate
      distinct dosage claims for the same drug (e.g. 50mg daily vs 200mg IV stat).
    - For INTERACTION: normalizes comma-separated drug pairs alphabetically so that
      "DrugA, DrugB" and "DrugB, DrugA" produce identical keys.
    """
    c_type = claim_type.value if isinstance(claim_type, SafetyClaimType) else str(claim_type).lower()

    if c_type == SafetyClaimType.INTERACTION.value:
        drugs = sorted(d.strip().casefold() for d in target.split(","))
        return f"interaction:{', '.join(drugs)}"

    norm_target = target.strip().casefold()
    norm_detail = " ".join(detail.strip().casefold().split())
    return f"{c_type}:{norm_target}:{norm_detail}"


def _parse_flag_key(flag: SafetyFlag) -> str:
    """Deterministic compatibility parser over existing human-readable flag claim strings."""
    if flag.claim.startswith("Drug interaction: "):
        target = flag.claim.removeprefix("Drug interaction: ").strip()
        return canonical_claim_key(SafetyClaimType.INTERACTION, target)
    return f"flag:{flag.claim.strip().casefold()}"


def partition_verdict_claims(
    verdict: SafetyVerdict,
) -> tuple[tuple[SafetyClaimCheck, ...], tuple[ExcludedClaim, ...]]:
    """Partition claims into verified safe vs excluded with canonical deduplication."""
    seen_keys: set[str] = set()
    verified_safe: list[SafetyClaimCheck] = []
    excluded: list[ExcludedClaim] = []

    # 1. Process structured checked_claims
    for check in verdict.checked_claims:
        key = canonical_claim_key(check.claim_type, check.target, check.detail)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        if check.status == SafetyClaimStatus.VERIFIED_SAFE:
            # Per-claim provenance requirement: non-empty verified citations
            if not check.citations:
                excluded.append(
                    ExcludedClaim(
                        claim_id=key,
                        claim=f"{check.claim_type.value}: {check.target}",
                        claim_type=check.claim_type,
                        status=SafetyClaimStatus.UNSUPPORTED,
                        reason="Claim marked safe but lacked verified citations/provenance.",
                        citations=(),
                    )
                )
            else:
                verified_safe.append(check)
        else:
            # UNSUPPORTED or FLAGGED -> excluded from asserted note
            excluded.append(
                ExcludedClaim(
                    claim_id=key,
                    claim=f"{check.claim_type.value}: {check.target}",
                    claim_type=check.claim_type,
                    status=check.status,
                    reason=check.detail,
                    citations=check.citations,
                )
            )

    # 2. Process flags using deterministic compatibility parser (deduplicating against checked_claims)
    for flag in verdict.flags:
        key = _parse_flag_key(flag)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        excluded.append(
            ExcludedClaim(
                claim_id=key,
                claim=flag.claim,
                claim_type=SafetyClaimType.INTERACTION,
                status=SafetyClaimStatus.FLAGGED,
                reason=flag.reason,
                citations=flag.citations,
            )
        )

    return tuple(verified_safe), tuple(excluded)


def serialize_supported_claims(
    supported_claims: tuple[SafetyClaimCheck, ...],
) -> str | None:
    """Serialize complete verified safe claims in deterministic order.

    Returns the serialized string if it fits completely within MAX_CASE_CHARACTERS.
    Returns None if the complete context cannot fit, triggering a fail-closed refusal.
    NEVER truncates or slices clinical text in the middle.
    """
    lines: list[str] = ["Verified Safe Clinical Findings:"]
    sorted_claims = sorted(
        supported_claims,
        key=lambda c: canonical_claim_key(c.claim_type, c.target, c.detail),
    )
    for claim in sorted_claims:
        lines.append(f"- [{claim.claim_type.value.upper()}] {claim.target}: {claim.detail}")

    serialized = "\n".join(lines)
    if len(serialized) > MAX_CASE_CHARACTERS:
        return None
    return serialized


@dataclass(frozen=True, slots=True)
class ExcludedClaim:
    """An unverified or flagged claim withheld from asserted text but retained for review."""

    claim_id: str
    claim: str
    claim_type: SafetyClaimType
    status: SafetyClaimStatus
    reason: str
    citations: tuple[Citation, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.claim_id, "claim_id", 500)
        require_text(self.claim, "claim", 4_000)
        require_text(self.reason, "reason", 4_000)
        if not isinstance(self.claim_type, SafetyClaimType):
            raise TypeError("claim_type must be a SafetyClaimType")
        if self.status not in (SafetyClaimStatus.UNSUPPORTED, SafetyClaimStatus.FLAGGED):
            raise ValueError("ExcludedClaim status must be UNSUPPORTED or FLAGGED")
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")


@dataclass(frozen=True, slots=True)
class DeferredClaim:
    """A VERIFIED_SAFE claim omitted from the draft note solely due to technical capacity limits."""

    claim_id: str
    claim: str
    claim_type: SafetyClaimType
    status: SafetyClaimStatus
    reason: str
    citations: tuple[Citation, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.claim_id, "claim_id", 500)
        require_text(self.claim, "claim", 4_000)
        require_text(self.reason, "reason", 4_000)
        if not isinstance(self.claim_type, SafetyClaimType):
            raise TypeError("claim_type must be a SafetyClaimType")
        if self.status != SafetyClaimStatus.VERIFIED_SAFE:
            raise ValueError("DeferredClaim status must be strictly VERIFIED_SAFE")
        if not isinstance(self.citations, tuple):
            raise TypeError("citations must be a tuple")


@dataclass(frozen=True, slots=True)
class ClinicalNoteDraft:
    """Typed, reviewable draft output produced by Documentation Drafter (AGT-03)."""

    workflow_id: UUID
    draft_id: str
    note: str
    asserted_claims: tuple[SafetyClaimCheck, ...]
    excluded_claims: tuple[ExcludedClaim, ...]
    deferred_claims: tuple[DeferredClaim, ...] = ()
    citations: tuple[Citation, ...] = ()
    refused: bool = False
    safety_status: SafetyStatus = SafetyStatus.SAFE
    can_proceed: bool = False
    requires_review: Literal[True] = True
    evidence_trace_ids: tuple[UUID, ...] = ()
    quarantined_chunks_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_digest(self.draft_id)
        require_text(self.note, "note", 64_000)
        if draft_digest(self.note) != self.draft_id:
            raise ValueError("draft_id must match draft_digest(note)")
        if self.requires_review is not True:
            raise ValueError("requires_review must be True")
        if type(self.refused) is not bool:
            raise TypeError("refused must be a boolean")
        if not isinstance(self.safety_status, SafetyStatus):
            raise TypeError("safety_status must be a SafetyStatus")
        if type(self.can_proceed) is not bool:
            raise TypeError("can_proceed must be a boolean")

        # Invariant: can_proceed can ONLY be True if safety_status is SAFE
        if self.can_proceed and self.safety_status != SafetyStatus.SAFE:
            raise ValueError("can_proceed cannot be True if safety_status is not SAFE")

        # Invariant: refused must match absence of citations and asserted claims
        if self.refused != (not self.citations and not self.asserted_claims):
            raise ValueError("refused must match absence of citations/claims")

        # Per-claim provenance & safety invariant
        for check in self.asserted_claims:
            if check.status != SafetyClaimStatus.VERIFIED_SAFE:
                raise ValueError(f"Asserted claim '{check.target}' is not VERIFIED_SAFE")
            if not check.citations:
                raise ValueError(f"Asserted claim '{check.target}' lacks required citations")

        # Excluded claim invariant
        for exc in self.excluded_claims:
            if exc.status not in (SafetyClaimStatus.UNSUPPORTED, SafetyClaimStatus.FLAGGED):
                raise ValueError(f"Excluded claim '{exc.claim_id}' has invalid status")

        # Deferred claim invariant
        for def_claim in self.deferred_claims:
            if def_claim.status != SafetyClaimStatus.VERIFIED_SAFE:
                raise ValueError(f"Deferred claim '{def_claim.claim_id}' must be VERIFIED_SAFE")

