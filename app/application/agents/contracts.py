"""Typed contracts for the Guideline Researcher and pipeline agents (AGT-01).

Pure standard-library dataclasses and enums. Clean Architecture boundary:
no framework, ORM, SDK or third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from app.application.clinical_tools.contracts import (
    MAX_CASE_CHARACTERS,
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
