"""Validate committed data against real corpus artifacts and #8 format extraction."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.application.evaluation.data import EvaluationSetupError, canonical_hash, normalized
from app.infrastructure.evaluation.catalog import FileEvaluationCatalog
from app.infrastructure.ingestion.extractors import MarkdownExtractor, PdfExtractor
from scripts.corpus import artifact_bytes, load_manifest

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data/evaluation/golden.v1.json"
CORPUS = ROOT / "data/corpus/manifest.json"


@pytest.mark.parametrize("version", [1, 2])
def test_actual_golden_set_counts_provenance_and_real_pdf_markdown_anchors(version: int) -> None:
    dataset_path = DATASET.with_name(f"golden.v{version}.json")
    dataset = FileEvaluationCatalog(str(dataset_path), str(CORPUS)).load()
    assert len([case for case in dataset.cases if not case.adversarial]) >= 25
    assert len([case for case in dataset.cases if case.adversarial]) >= 5
    assert len([case for case in dataset.cases if "injection" in case.safety_tags]) >= 3
    assert {
        "direct_injection",
        "indirect_injection",
        "conflicting_sources",
        "ambiguous",
        "out_of_corpus",
    }.issubset({case.category for case in dataset.cases})
    manifest = load_manifest(CORPUS)
    texts = {}
    formats = set()
    for document in manifest["documents"]:
        content = artifact_bytes(document, CORPUS.parent)
        extractor = (
            PdfExtractor(500, 2_000_000) if document["format"] == "pdf" else MarkdownExtractor()
        )
        texts[document["id"]] = normalized(
            " ".join(block.text for block in extractor.extract(content))
        )
        formats.add(document["format"])
    assert formats == {"pdf", "markdown"}
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    for fixture in raw["fixtures"]:
        texts[fixture["id"]] = normalized(
            " ".join(
                block.text
                for block in MarkdownExtractor().extract(
                    (dataset_path.parent / fixture["path"]).read_bytes()
                )
            )
        )
    for case in dataset.cases:
        for reference in case.evidence:
            assert normalized(reference.quote) in texts[reference.document_id], case.id
    assert dataset.corpus_version == manifest["corpus_version"]
    assert sum(source.trusted for source in dataset.sources) == 36
    assert sum(not source.trusted for source in dataset.sources) == (2 if version == 1 else 3)
    if version == 2:
        assert sum(case.containment for case in dataset.cases) == 8
        assert all(case.containment for case in dataset.cases if "injection" in case.safety_tags)


@pytest.mark.parametrize(
    "fault",
    [
        "checksum",
        "few_qa",
        "few_adversarial",
        "injections",
        "missing_source",
        "duplicate_case",
        "invented_refusal",
        "corpus_version",
        "path_escape",
    ],
)
def test_invalid_or_unpinned_data_is_rejected(tmp_path: Path, fault: str) -> None:
    directory = tmp_path / "evaluation"
    shutil.copytree(DATASET.parent, directory)
    path = directory / DATASET.name
    raw = json.loads(path.read_text(encoding="utf-8"))
    if fault == "few_qa":
        raw["cases"] = raw["cases"][6:]
    elif fault == "few_adversarial":
        raw["cases"] = raw["cases"][:-5]
    elif fault == "injections":
        for case in raw["cases"]:
            case["safety_tags"] = [tag for tag in case["safety_tags"] if tag != "injection"]
    elif fault == "missing_source":
        raw["cases"][0]["expected_document_ids"] = ["nonexistent"]
    elif fault == "duplicate_case":
        raw["cases"][1]["id"] = raw["cases"][0]["id"]
    elif fault == "invented_refusal":
        raw["cases"][0]["should_refuse"] = True
    elif fault == "corpus_version":
        raw["corpus_version"] = "not-installed"
    elif fault == "path_escape":
        raw["fixtures"][0]["path"] = "../secret.md"
    digest = canonical_hash(
        {key: value for key, value in raw.items() if key not in {"sha256", "dataset_version"}}
    )
    raw.update(sha256=digest, dataset_version="healthcare-qa-v1-" + digest[:16])
    if fault == "checksum":
        raw["cases"][0]["query"] += " unversioned edit"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EvaluationSetupError):
        FileEvaluationCatalog(str(path), str(CORPUS)).load()


@pytest.mark.parametrize("fault", ["missing", "untyped", "normal_case", "clean_source"])
def test_v2_cannot_silently_drop_or_fake_a_containment_exercise(tmp_path: Path, fault: str) -> None:
    path = tmp_path / "golden.v2.json"
    shutil.copytree(DATASET.parent / "fixtures", tmp_path / "fixtures")
    raw = json.loads(DATASET.with_name("golden.v2.json").read_text(encoding="utf-8"))
    attack = next(case for case in raw["cases"] if case["category"] == "indirect_injection")
    if fault == "missing":
        attack.pop("containment")
    elif fault == "untyped":
        attack["containment"] = "true"
    elif fault == "normal_case":
        raw["cases"][0]["containment"] = True
    else:
        attack["expected_document_ids"] = ["public-pmc11166373"]
        attack["evidence"] = [{"document_id": "public-pmc11166373", "quote": "doxycycline"}]
    digest = canonical_hash(
        {key: value for key, value in raw.items() if key not in {"sha256", "dataset_version"}}
    )
    raw.update(sha256=digest, dataset_version="healthcare-qa-v2-" + digest[:16])
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EvaluationSetupError):
        FileEvaluationCatalog(str(path), str(CORPUS)).load()
