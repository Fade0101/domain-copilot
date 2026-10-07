"""Framework-free typed I/O. Identity, roles and approval flags are never arguments.

The infrastructure codec exports these dataclasses as strict JSON schemas for
the existing #7 ToolDefinition/ToolCall interfaces. Constructors also validate
in-process callers, which cannot bypass validation by avoiding JSON.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from app.application.clinical_tools.errors import ToolInputError
from app.application.qa.grounding import REFUSAL
from app.application.retrieval.dto import Citation
from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS

MAX_ARGUMENT_BYTES = 32_768
MAX_CASE_CHARACTERS = 4_000
MAX_NOTE_CHARACTERS = 64_000


class ToolName(StrEnum):
    SEARCH_CORPUS = "search_corpus"
    RETRIEVE_DRUG_INFO = "retrieve_drug_info"
    CHECK_INTERACTIONS = "check_interactions"
    VALIDATE_DOSAGE = "validate_dosage"
    DRAFT_CLINICAL_NOTE = "draft_clinical_note"
    FINALIZE_CLINICAL_NOTE = "finalize_clinical_note"


class ToolOwner(StrEnum):
    GUIDELINE_RESEARCHER = "guideline_researcher"
    SAFETY_CHECKER = "safety_checker"
    DOCUMENTATION_DRAFTER = "documentation_drafter"
    ORCHESTRATOR = "orchestrator"


class DrugTopic(StrEnum):
    OVERVIEW = "overview"
    CONTRAINDICATIONS = "contraindications"
    INTERACTIONS = "interactions"
    DOSAGE = "dosage"


class DosageEvidenceStatus(StrEnum):
    SUPPORTED_BY_CORPUS = "SUPPORTED_BY_CORPUS"
    NOT_VERIFIED = "NOT_VERIFIED"


def require_text(value: str, field: str, maximum: int, *, optional: bool = False) -> None:
    if not isinstance(value, str) or len(value) > maximum or (not optional and not value.strip()):
        raise ToolInputError(f"Invalid {field}")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ToolInputError(f"Invalid {field}")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ToolInputError(f"Invalid {field}") from exc


def require_uuid(value: UUID, field: str) -> None:
    if not isinstance(value, UUID):
        raise ToolInputError(f"Invalid {field}")


def require_digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ToolInputError("Invalid draft_id")


def draft_digest(original_note: str) -> str:
    """Bind an approval to the exact UTF-8 draft, without normalizing or editing it."""
    require_text(original_note, "original_note", MAX_NOTE_CHARACTERS)
    return hashlib.sha256(original_note.encode("utf-8")).hexdigest()


def _names(values: tuple[str, ...], field: str, *, minimum: int, maximum: int) -> None:
    if not isinstance(values, tuple) or not minimum <= len(values) <= maximum:
        raise ToolInputError(f"Invalid {field}")
    for value in values:
        require_text(value, field, 100)
    if len({value.strip().casefold() for value in values}) != len(values):
        raise ToolInputError(f"Duplicate {field}")


def _citations(values: tuple[Citation, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > 12:
        raise ToolInputError("Invalid citations")
    identifiers = set()
    for item in values:
        if not isinstance(item, Citation):
            raise ToolInputError("Invalid citation")
        require_uuid(item.document_id, "document_id")
        require_uuid(item.chunk_id, "chunk_id")
        require_text(item.document_name, "document_name", 255)
        require_text(item.text_snippet, "text_snippet", 24_000)
        if item.section is not None:
            require_text(item.section, "section", 10_000, optional=True)
        if item.page is not None and (type(item.page) is not int or item.page < 1):
            raise ToolInputError("Invalid citation page")
        if (
            type(item.relevance_score) not in {int, float}
            or not math.isfinite(item.relevance_score)
            or not 0 <= item.relevance_score <= 1
            or item.chunk_id in identifiers
        ):
            raise ToolInputError("Invalid citation score or duplicate chunk")
        identifiers.add(item.chunk_id)


@dataclass(frozen=True, slots=True)
class SearchCorpusInput:
    query: str

    def __post_init__(self) -> None:
        require_text(self.query, "query", MAX_QUERY_CHARACTERS)


@dataclass(frozen=True, slots=True)
class RetrieveDrugInfoInput:
    drug: str
    topic: DrugTopic = DrugTopic.OVERVIEW

    def __post_init__(self) -> None:
        require_text(self.drug, "drug", 100)
        if not isinstance(self.topic, DrugTopic):
            raise ToolInputError("Invalid drug topic")


@dataclass(frozen=True, slots=True)
class CheckInteractionsInput:
    drugs: tuple[str, ...]
    conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _names(self.drugs, "drugs", minimum=1, maximum=8)
        _names(self.conditions, "conditions", minimum=0, maximum=5)
        if len(self.drugs) + len(self.conditions) < 2:
            raise ToolInputError("Interactions require at least two drugs/conditions")


@dataclass(frozen=True, slots=True)
class ValidateDosageInput:
    drug: str
    dosage_claim: str

    def __post_init__(self) -> None:
        require_text(self.drug, "drug", 100)
        require_text(self.dosage_claim, "dosage_claim", 1_500)


@dataclass(frozen=True, slots=True)
class DraftClinicalNoteInput:
    clinical_question: str
    case_summary: str

    def __post_init__(self) -> None:
        require_text(self.clinical_question, "clinical_question", MAX_QUERY_CHARACTERS)
        require_text(self.case_summary, "case_summary", MAX_CASE_CHARACTERS)


@dataclass(frozen=True, slots=True)
class FinalizeClinicalNoteInput:
    workflow_id: UUID
    draft_id: str
    approval_id: UUID

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_uuid(self.approval_id, "approval_id")
        require_digest(self.draft_id)


@dataclass(frozen=True, slots=True)
class SearchCorpusOutput:
    citations: tuple[Citation, ...]
    refused: bool
    evidence_trace_id: UUID

    def __post_init__(self) -> None:
        _citations(self.citations)
        require_uuid(self.evidence_trace_id, "evidence_trace_id")
        if type(self.refused) is not bool or self.refused != (not self.citations):
            raise ToolInputError("Invalid search result")


@dataclass(frozen=True, slots=True)
class EvidenceOutput:
    evidence: str
    citations: tuple[Citation, ...]
    refused: bool
    evidence_trace_id: UUID

    def __post_init__(self) -> None:
        require_text(self.evidence, "evidence", MAX_NOTE_CHARACTERS)
        _citations(self.citations)
        require_uuid(self.evidence_trace_id, "evidence_trace_id")
        if (
            type(self.refused) is not bool
            or self.refused != (not self.citations)
            or (self.refused and self.evidence != REFUSAL)
        ):
            raise ToolInputError("Invalid evidence result")


@dataclass(frozen=True, slots=True)
class RetrieveDrugInfoOutput(EvidenceOutput):
    drug: str
    topic: DrugTopic

    def __post_init__(self) -> None:
        EvidenceOutput.__post_init__(self)
        RetrieveDrugInfoInput(self.drug, self.topic)


@dataclass(frozen=True, slots=True)
class CheckInteractionsOutput(EvidenceOutput):
    drugs: tuple[str, ...]
    conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        EvidenceOutput.__post_init__(self)
        CheckInteractionsInput(self.drugs, self.conditions)


@dataclass(frozen=True, slots=True)
class ValidateDosageOutput(EvidenceOutput):
    drug: str
    dosage_claim: str
    status: DosageEvidenceStatus

    def __post_init__(self) -> None:
        EvidenceOutput.__post_init__(self)
        ValidateDosageInput(self.drug, self.dosage_claim)
        if not isinstance(self.status, DosageEvidenceStatus) or self.refused != (
            self.status == DosageEvidenceStatus.NOT_VERIFIED
        ):
            raise ToolInputError("Invalid dosage result")


@dataclass(frozen=True, slots=True)
class DraftClinicalNoteOutput:
    workflow_id: UUID
    draft_id: str
    note: str
    citations: tuple[Citation, ...]
    refused: bool
    evidence_trace_id: UUID
    requires_review: Literal[True] = True

    def __post_init__(self) -> None:
        require_uuid(self.workflow_id, "workflow_id")
        require_uuid(self.evidence_trace_id, "evidence_trace_id")
        _citations(self.citations)
        require_digest(self.draft_id)
        if (
            draft_digest(self.note) != self.draft_id
            or self.requires_review is not True
            or type(self.refused) is not bool
            or self.refused != (not self.citations)
        ):
            raise ToolInputError("Invalid clinical draft")


@dataclass(frozen=True, slots=True)
class FinalizeClinicalNoteOutput:
    note_id: UUID
    workflow_id: UUID
    draft_id: str
    approval_id: UUID
    note: str
    finalized_by: UUID
    finalized_at: datetime
    created: bool

    def __post_init__(self) -> None:
        for value in (self.note_id, self.workflow_id, self.approval_id, self.finalized_by):
            require_uuid(value, "final note identity")
        require_digest(self.draft_id)
        require_text(self.note, "final note", MAX_NOTE_CHARACTERS)
        if (
            not isinstance(self.finalized_at, datetime)
            or self.finalized_at.tzinfo is None
            or type(self.created) is not bool
        ):
            raise ToolInputError("Invalid final note result")


ToolInput = (
    SearchCorpusInput
    | RetrieveDrugInfoInput
    | CheckInteractionsInput
    | ValidateDosageInput
    | DraftClinicalNoteInput
    | FinalizeClinicalNoteInput
)
ToolOutput = (
    SearchCorpusOutput
    | RetrieveDrugInfoOutput
    | CheckInteractionsOutput
    | ValidateDosageOutput
    | DraftClinicalNoteOutput
    | FinalizeClinicalNoteOutput
)
