"""Corpus identity, actual-file verification and delegation to the existing uploader."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import corpus
from tests.support.corpus_fixtures import BuiltCorpus
from tests.support.corpus_fixtures import built_corpus as built_corpus


def test_actual_corpus_files_exceed_both_size_floors(built_corpus: BuiltCorpus) -> None:
    summary = corpus.verify(
        built_corpus.manifest, built_corpus.source_directory, built_corpus.directory
    )
    assert summary["document_count"] == 36
    assert summary["page_count"] >= 150
    assert summary["public_documents"] == 24
    assert summary["synthetic_documents"] == 12
    assert summary["pdf_documents"] == 8
    assert summary["markdown_documents"] == 28


def test_provenance_edit_requires_a_new_corpus_version(
    built_corpus: BuiltCorpus, tmp_path: Path
) -> None:
    manifest = copy.deepcopy(built_corpus.manifest)
    manifest["documents"][0]["publication_version"] = "changed"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="version does not match"):
        corpus.load_manifest(path)


@pytest.mark.parametrize(
    "field",
    ["title", "publisher", "source_url", "publication_date", "retrieval_date", "usage_notes"],
)
def test_sources_require_complete_provenance(
    built_corpus: BuiltCorpus, tmp_path: Path, field: str
) -> None:
    manifest = copy.deepcopy(built_corpus.manifest)
    del manifest["documents"][0][field]
    corpus.stamp_manifest(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="complete provenance"):
        corpus.load_manifest(path)


def test_duplicate_content_cannot_pad_document_count(
    built_corpus: BuiltCorpus, tmp_path: Path
) -> None:
    manifest = copy.deepcopy(built_corpus.manifest)
    manifest["documents"][1]["source_sha256"] = manifest["documents"][0]["source_sha256"]
    corpus.stamp_manifest(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate document"):
        corpus.load_manifest(path)


def test_duplicate_json_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate manifest field"):
        corpus.load_manifest(path)


def test_all_authoring_files_must_be_in_manifest(built_corpus: BuiltCorpus, tmp_path: Path) -> None:
    shutil.copytree(built_corpus.source_directory / "sources", tmp_path / "sources")
    (tmp_path / "sources/synthetic/unlisted.md").write_text("Unlisted source", encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted corpus files"):
        corpus.verify_source_inventory(built_corpus.manifest, tmp_path)


def test_edited_authoring_source_fails_before_build_writes(
    built_corpus: BuiltCorpus, tmp_path: Path
) -> None:
    shutil.copytree(built_corpus.source_directory / "sources", tmp_path / "sources")
    source = tmp_path / built_corpus.manifest["documents"][0]["source_path"]
    source.write_bytes(source.read_bytes() + b"Changed source.\n")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Source checksum mismatch"):
        corpus.build(built_corpus.manifest, tmp_path, output)
    assert not output.exists()


@pytest.mark.parametrize("change", ["corrupt", "missing", "extra"])
def test_changed_build_artifacts_cannot_pass_verification(
    built_corpus: BuiltCorpus, tmp_path: Path, change: str
) -> None:
    output = tmp_path / "output"
    shutil.copytree(built_corpus.directory, output)
    artifact = output / built_corpus.manifest["documents"][0]["artifact_path"]
    if change == "corrupt":
        artifact.write_bytes(artifact.read_bytes() + b"Corrupted artifact.\n")
    elif change == "missing":
        artifact.unlink()
    else:
        (output / "extra.md").write_text("Unlisted artifact", encoding="utf-8")
    with pytest.raises(ValueError, match="differs|missing or unexpected"):
        corpus.verify(built_corpus.manifest, built_corpus.source_directory, output)


def test_manifest_page_counts_do_not_override_actual_files(built_corpus: BuiltCorpus) -> None:
    manifest = copy.deepcopy(built_corpus.manifest)
    manifest["documents"][0]["page_count"] += 150
    with pytest.raises(ValueError, match="Measured artifact differs"):
        corpus.verify(manifest, built_corpus.source_directory, built_corpus.directory)


def test_build_cannot_write_into_versioned_corpus(built_corpus: BuiltCorpus) -> None:
    with pytest.raises(ValueError, match="must not overwrite"):
        corpus.build(
            built_corpus.manifest, built_corpus.source_directory, built_corpus.source_directory
        )


@pytest.mark.parametrize("words,pages", [(50, 1), (500, 1), (501, 2), (1000, 2), (1001, 3)])
def test_markdown_uses_documented_500_word_virtual_pages(words: int, pages: int) -> None:
    measured = corpus.measure(("evidence " * words).encode(), "markdown")
    assert measured["page_count"] == pages
    assert measured["word_count"] == words


def test_markdown_punctuation_does_not_inflate_page_count() -> None:
    measured = corpus.measure(("evidence " * 50 + "# - | * " * 5000).encode(), "markdown")
    assert measured["word_count"] == 50
    assert measured["page_count"] == 1


def test_source_hashes_are_portable_across_git_line_endings(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"# Heading\r\n\r\nEvidence\rLast line\n")
    assert corpus.normalized_source(source) == b"# Heading\n\nEvidence\nLast line\n"


@pytest.mark.parametrize("relative", ["../outside.md", "/outside.md", "C:/outside.md", "a\\b.md"])
def test_manifest_paths_cannot_escape_root(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError, match="safe relative"):
        corpus.safe_path(tmp_path, relative)


def test_corpus_ingest_delegates_to_ticket8_and_propagates_failure(
    built_corpus: BuiltCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def upload(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(subprocess, "run", upload)
    result = corpus.ingest(
        built_corpus.manifest,
        built_corpus.source_directory,
        built_corpus.directory,
        "http://127.0.0.1:8000",
        1800,
    )
    assert result == 3
    assert len(calls) == 1
    assert Path(calls[0][1]) == corpus.ROOT / "scripts/ingest_documents.py"
    assert calls[0][2:9] == [
        "--api-url",
        "http://127.0.0.1:8000",
        "--wait",
        "--timeout",
        "1800",
        "--version",
        "1",
    ]
    assert len(calls[0][9:]) == 36
