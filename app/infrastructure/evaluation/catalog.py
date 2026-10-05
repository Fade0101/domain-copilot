"""Load immutable golden data and its corpus/fixture checksums, never client paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.application.evaluation.data import (
    EvaluationSetupError,
    EvidenceReference,
    GoldenCase,
    GoldenSet,
    SourcePin,
    canonical_hash,
    validate_case_id,
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


class FileEvaluationCatalog:
    def __init__(self, dataset_path: str, corpus_path: str) -> None:
        self.dataset_path = Path(dataset_path)
        self.corpus_path = Path(corpus_path)

    def load(self) -> GoldenSet:
        try:
            return self._load()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise EvaluationSetupError("INVALID_EVALUATION_DATA") from None

    def _load(self) -> GoldenSet:
        raw = load_json(self.dataset_path)
        corpus = load_json(self.corpus_path)
        digest = canonical_hash(
            {k: v for k, v in raw.items() if k not in {"sha256", "dataset_version"}}
        )
        if raw["schema_version"] != 1 or raw["sha256"] != digest:
            raise ValueError("Golden set checksum mismatch")
        if raw["dataset_version"] != "healthcare-qa-v1-" + digest[:16]:
            raise ValueError("Golden set version mismatch")
        corpus_hash = canonical_hash(
            {k: v for k, v in corpus.items() if k not in {"sha256", "corpus_version"}}
        )
        if (
            corpus["sha256"] != corpus_hash
            or raw["corpus_version"] != corpus["corpus_version"]
            or raw["corpus_sha256"] != corpus_hash
        ):
            raise ValueError("Corpus pin mismatch")
        sources = [
            SourcePin(
                d["id"],
                d["sha256"],
                "application/pdf" if d["format"] == "pdf" else "text/markdown",
                d["ingestion_version"],
                True,
            )
            for d in corpus["documents"]
        ]
        for fixture in raw["fixtures"]:
            path = (self.dataset_path.parent / fixture["path"]).resolve()
            if not path.is_relative_to((self.dataset_path.parent / "fixtures").resolve()):
                raise ValueError("Invalid fixture path")
            content = path.read_text(encoding="utf-8").encode("utf-8")
            if hashlib.sha256(content).hexdigest() != fixture["sha256"]:
                raise ValueError("Fixture checksum mismatch")
            if (
                not content.startswith(b"# SYNTHETIC EVALUATION ATTACK FIXTURE")
                or path.suffix != ".md"
            ):
                raise ValueError("Fixtures must be labelled Markdown")
            sources.append(SourcePin(fixture["id"], fixture["sha256"], "text/markdown", 1, False))
        source_ids = {source.id for source in sources}
        if len(source_ids) != len(sources):
            raise ValueError("Duplicate source ID")
        cases: list[GoldenCase] = []
        for row in raw["cases"]:
            case = GoldenCase(
                id=row["id"],
                category=row["category"],
                query=row["query"],
                expected_document_ids=tuple(row["expected_document_ids"]),
                expected_chunk_ids=tuple(row["expected_chunk_ids"]),
                evidence=tuple(EvidenceReference(**item) for item in row["evidence"]),
                expected_answer=tuple(row["expected_answer"]),
                expected_behavior=row["expected_behavior"],
                required_citations=row["required_citations"],
                should_refuse=row["should_refuse"],
                safety_tags=tuple(row["safety_tags"]),
                adversarial=row["adversarial"],
                notes=row["notes"],
            )
            if (
                not validate_case_id(case.id)
                or not 1 <= len(case.query) <= 4000
                or type(case.should_refuse) is not bool
                or type(case.adversarial) is not bool
                or type(case.required_citations) is not int
                or not 0 <= case.required_citations <= 5
                or not set(case.expected_document_ids).issubset(source_ids)
                or not set(case.safety_tags).issubset(
                    {"dosage", "contraindication", "interaction", "injection", "documentation"}
                )
                or any(
                    not ref.quote.strip() or ref.document_id not in case.expected_document_ids
                    for ref in case.evidence
                )
                or (
                    not case.should_refuse
                    and (
                        not case.expected_answer or not case.evidence or case.required_citations < 1
                    )
                )
                or (case.should_refuse and (case.required_citations or case.expected_answer))
                or case.expected_behavior != ("refuse" if case.should_refuse else "answer")
            ):
                raise ValueError("Invalid golden case")
            cases.append(case)
        if len({case.id for case in cases}) != len(cases) or len(cases) > 200:
            raise ValueError("Duplicate or excessive cases")
        normal = [c for c in cases if not c.adversarial]
        attacks = [c for c in cases if c.adversarial]
        if len(normal) < 25 or len(attacks) < 5:
            raise ValueError("Insufficient golden set coverage")
        if sum("injection" in c.safety_tags for c in attacks) < 3 or not any(
            c.category == "indirect_injection" for c in attacks
        ):
            raise ValueError("Insufficient injection coverage")
        if not {"dosage", "contraindication", "interaction"}.issubset(
            {tag for c in cases for tag in c.safety_tags}
        ):
            raise ValueError("Missing clinical safety cases")
        return GoldenSet(
            raw["dataset_version"],
            digest,
            corpus["corpus_version"],
            corpus_hash,
            canonical_hash(raw["fixtures"]),
            tuple(sources),
            tuple(cases),
        )
