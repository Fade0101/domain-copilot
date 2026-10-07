"""SDK-free evaluation records. Corpus IDs are resolved to real indexed UUIDs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.errors import ApplicationError

EVALUATION_OPERATION = "evaluation.run"
EVALUATOR_VERSION = "extractive-evaluation-v2-containment"


class EvaluationSetupError(ApplicationError):
    """A version, corpus, identity or persisted-result prerequisite is invalid."""


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass(frozen=True)
class EvidenceReference:
    document_id: str
    quote: str


@dataclass(frozen=True)
class GoldenCase:
    id: str
    category: str
    query: str
    expected_document_ids: tuple[str, ...]
    expected_chunk_ids: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]
    expected_answer: tuple[str, ...]
    expected_behavior: str
    required_citations: int
    should_refuse: bool
    safety_tags: tuple[str, ...]
    adversarial: bool
    notes: str
    containment: bool = False


@dataclass(frozen=True)
class ContainmentFixture:
    """IDs of evaluation-only PostgreSQL records, never a claimed approval."""

    workflow_id: UUID
    pending_approval_id: UUID
    rejected_approval_id: UUID
    missing_approval_id: UUID
    draft_id: str


@dataclass(frozen=True)
class SourcePin:
    id: str
    sha256: str
    media_type: str
    version: int
    trusted: bool


@dataclass(frozen=True)
class GoldenSet:
    version: str
    sha256: str
    corpus_version: str
    corpus_sha256: str
    fixtures_sha256: str
    sources: tuple[SourcePin, ...]
    cases: tuple[GoldenCase, ...]

    @property
    def pins(self) -> dict[str, str]:
        return {
            "dataset_version": self.version,
            "dataset_sha256": self.sha256,
            "corpus_version": self.corpus_version,
            "corpus_sha256": self.corpus_sha256,
            "fixtures_sha256": self.fixtures_sha256,
            "evaluator_version": EVALUATOR_VERSION,
        }


@dataclass(frozen=True)
class IndexedEvidence:
    chunk_id: str
    document_id: str
    source_id: str
    document_name: str
    section: str | None
    page: int | None
    text: str
    trusted: bool

    def matches_citation(self, citation: dict[str, Any]) -> bool:
        return all(
            citation.get(key) == expected
            for key, expected in {
                "chunk_id": self.chunk_id,
                "document_id": self.document_id,
                "document_name": self.document_name,
                "section": self.section,
                "page": self.page,
                "text_snippet": self.text,
            }.items()
        )


@dataclass(frozen=True)
class EvidenceSnapshot:
    fingerprint: str
    chunks: dict[str, IndexedEvidence]
    documents: dict[str, str]
    ingestion_config: dict[str, Any]

    def expected_chunks(self, case: GoldenCase) -> set[str]:
        found = set(case.expected_chunk_ids)
        if not found.issubset(self.chunks):
            raise EvaluationSetupError("EXPECTED_CHUNK_MISSING")
        for reference in case.evidence:
            matches = {
                chunk.chunk_id
                for chunk in self.chunks.values()
                if chunk.source_id == reference.document_id
                and normalized(reference.quote) in normalized(chunk.text)
            }
            if not matches:
                raise EvaluationSetupError("EXPECTED_EVIDENCE_MISSING")
            found.update(matches)
        return found


def validate_case_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value))
