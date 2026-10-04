"""Build, verify and ingest the versioned healthcare evidence corpus.

Build and verification are offline. Ingestion delegates to Ticket #8's existing
authenticated upload CLI; this module does not implement another pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import textwrap
from datetime import date
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data/corpus/manifest.json"
MIN_DOCUMENTS = 30
MIN_PAGES = 150
WORDS_PER_PAGE = 500
PDF_RENDERER = "synthetic-text-pdf-v1"
TOPICS = {"guidelines", "contraindications", "interactions", "clinical_documentation"}
EVIDENCE_ROLES = {"positive", "negative", "conflicting", "insufficient"}
WORD_PATTERN = r"\b\w+(?:['’\-]\w+)*\b"
PAGE_COUNTING = {
    "pdf": "Physical pages measured with pypdf; every page must contain extractable text.",
    "markdown": "Ceiling of Unicode word count / 500, minimum one page; not publisher pagination.",
    "word_pattern": WORD_PATTERN,
    "words_per_markdown_page": WORDS_PER_PAGE,
    "included_text": "All document text, including headings, references and attribution.",
}
SOURCE_CHECKSUM = "SHA-256 of UTF-8 text with CRLF/CR normalized to LF; no byte-order mark."
_WORDS = re.compile(WORD_PATTERN)
_HASH = re.compile(r"[0-9a-f]{64}")


def normalized_source(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"Source must not contain a UTF-8 byte-order mark: {path.name}")
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise ValueError(f"Source contains binary data: {path.name}")
    return text.encode("utf-8")


def safe_path(root: Path, relative: str) -> Path:
    """Confine manifest paths, including symlink resolution, to the declared root."""
    value = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or value.is_absolute()
        or ".." in value.parts
        or relative != value.as_posix()
        or value == PurePosixPath(".")
    ):
        raise ValueError("Manifest paths must be safe relative POSIX paths")
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    if not path.is_relative_to(resolved_root) or path == resolved_root:
        raise ValueError("Manifest path escapes its corpus directory")
    return path


def manifest_digest(manifest: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in manifest.items() if key not in {"corpus_version", "sha256"}
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp_manifest(manifest: dict[str, Any]) -> None:
    digest = manifest_digest(manifest)
    manifest["sha256"] = digest
    manifest["corpus_version"] = "healthcare-evidence-v1-" + digest[:16]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest field: {key}")
        result[key] = value
    return result


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("Unsupported corpus manifest schema")
    if manifest.get("page_counting") != PAGE_COUNTING:
        raise ValueError("Unsupported page-counting definition")
    if manifest.get("source_checksum") != SOURCE_CHECKSUM:
        raise ValueError("Unsupported source-checksum definition")
    digest = manifest_digest(manifest)
    if (
        manifest.get("sha256") != digest
        or manifest.get("corpus_version") != "healthcare-evidence-v1-" + digest[:16]
    ):
        raise ValueError("Corpus version does not match its manifest content")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) < MIN_DOCUMENTS:
        raise ValueError("The corpus must contain at least 30 documents")
    identifiers: set[str] = set()
    sources: set[str] = set()
    artifacts: set[str] = set()
    content_hashes: set[str] = set()
    source_hashes: set[str] = set()
    topics: set[str] = set()
    evidence_roles: set[str] = set()
    required_text = {
        "id",
        "title",
        "publisher",
        "source_url",
        "publication_date",
        "publication_version",
        "retrieval_date",
        "classification",
        "license_name",
        "license_notice",
        "usage_notes",
        "source_path",
        "artifact_path",
        "format",
        "source_sha256",
        "sha256",
        "data_scope",
    }
    for document in documents:
        if not isinstance(document, dict) or any(
            not isinstance(document.get(key), str) or not document[key].strip()
            for key in required_text
        ):
            raise ValueError("Every source requires complete provenance and artifact metadata")
        if document["classification"] not in {"public", "synthetic"}:
            raise ValueError("Each document must be classified public or synthetic")
        if document["format"] not in {"pdf", "markdown"}:
            raise ValueError("Only PDF and Markdown are corpus ingestion formats")
        suffix = ".pdf" if document["format"] == "pdf" else ".md"
        if Path(document["artifact_path"]).suffix != suffix:
            raise ValueError("Artifact extension does not match its declared format")
        safe_path(path.parent, document["source_path"])
        safe_path(path.parent, document["artifact_path"])
        if not document["source_path"].startswith(f"sources/{document['classification']}/"):
            raise ValueError("Source location must agree with its public/synthetic classification")
        if Path(document["source_path"]).suffix != ".md":
            raise ValueError("Versioned authoring sources must be Markdown")
        published = date.fromisoformat(document["publication_date"])
        retrieved = date.fromisoformat(document["retrieval_date"])
        if published > retrieved:
            raise ValueError("A source cannot be retrieved before publication")
        for key in ("source_sha256", "sha256"):
            if not _HASH.fullmatch(document[key]):
                raise ValueError("Invalid source or artifact SHA-256")
        for key in ("page_count", "word_count", "byte_count", "ingestion_version"):
            if type(document.get(key)) is not int or document[key] < 1:
                raise ValueError("Artifact measurements and ingestion version must be positive")
        if document["format"] == "pdf" and document.get("renderer") != PDF_RENDERER:
            raise ValueError("Unknown deterministic PDF renderer")
        for value, seen in [
            (document["id"], identifiers),
            (document["source_path"], sources),
            (document["artifact_path"], artifacts),
            (document["sha256"], content_hashes),
            (document["source_sha256"], source_hashes),
        ]:
            if value in seen:
                raise ValueError("Duplicate document, path or artifact content in the corpus")
            seen.add(value)
        for key, permitted, collected in [
            ("topics", TOPICS, topics),
            ("evidence_roles", EVIDENCE_ROLES, evidence_roles),
        ]:
            labels = document.get(key)
            if (
                not isinstance(labels, list)
                or not labels
                or any(not isinstance(label, str) or label not in permitted for label in labels)
            ):
                raise ValueError("Missing or unknown corpus coverage labels")
            collected.update(labels)
        if document["classification"] == "public":
            acquisition = document.get("acquisition", {})
            if (
                not document["source_url"].startswith("https://")
                or not isinstance(document.get("license_url"), str)
                or not document["license_url"].startswith("https://")
                or not isinstance(acquisition, dict)
                or not isinstance(acquisition.get("response_sha256"), str)
                or not _HASH.fullmatch(acquisition["response_sha256"])
                or not acquisition.get("url", "").startswith("https://")
            ):
                raise ValueError("Public sources require verifiable acquisition and usage records")
    if topics != TOPICS or evidence_roles != EVIDENCE_ROLES:
        raise ValueError("The corpus does not cover all required topics and evidence roles")
    return manifest


def render_pdf(source: bytes, title: str) -> bytes:
    """Deterministic, text-based PDFs from explicitly synthetic ASCII sources.

    No timestamps, random document IDs, network assets or additional dependencies
    enter the output. pypdf's version is already pinned by Ticket #8.
    """
    text = source.decode("ascii")
    title.encode("ascii")
    if "SYNTHETIC TRAINING DOCUMENT" not in text:
        raise ValueError("The PDF renderer accepts labelled synthetic training sources only")
    lines: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = " ".join(paragraph.split())
        if paragraph.startswith("#"):
            paragraph = paragraph.lstrip("# ").upper()
        lines.extend(
            textwrap.wrap(paragraph, width=86, break_long_words=False, break_on_hyphens=False)
        )
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise ValueError("Cannot render an empty synthetic document")
    pages = [lines[index : index + 43] for index in range(0, len(lines), 43)]
    writer = PdfWriter()
    writer.add_metadata(
        {"/Title": title, "/Subject": "SYNTHETIC TRAINING DOCUMENT; no patient data"}
    )
    for number, content in enumerate(pages, 1):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        commands = ["BT /F1 10 Tf 44 752 Td 15 TL"]
        for line in ["SYNTHETIC TRAINING DOCUMENT - NOT A REAL CLINICAL POLICY", "", *content]:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*")
        commands.extend(
            ["ET", "BT /F1 9 Tf 44 34 Td", f"(SYNTHETIC - Page {number} of {len(pages)}) Tj", "ET"]
        )
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def measure(data: bytes, format_name: str) -> dict[str, int | str]:
    if not data or len(data) > 10_485_760:
        raise ValueError("Corpus artifact is empty or exceeds Ticket #8's default 10 MiB limit")
    if format_name == "pdf":
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 500:
            raise ValueError("PDF must be readable and within Ticket #8's default page limit")
        page_text = [page.extract_text() or "" for page in reader.pages]
        if any(not text.strip() for text in page_text):
            raise ValueError("Blank or image-only pages do not count toward this corpus")
        text = "\n".join(page_text)
        page_count = len(page_text)
    elif format_name == "markdown":
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError("Markdown artifact contains binary data")
        page_count = max(1, math.ceil(len(_WORDS.findall(text)) / WORDS_PER_PAGE))
    else:
        raise ValueError("Unsupported corpus format")
    words = len(_WORDS.findall(text))
    if words < 50:
        raise ValueError("A corpus document must contain substantive text")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "word_count": words,
        "page_count": page_count,
    }


def artifact_bytes(document: dict[str, Any], corpus_root: Path) -> bytes:
    source = normalized_source(safe_path(corpus_root, document["source_path"]))
    if hashlib.sha256(source).hexdigest() != document["source_sha256"]:
        raise ValueError(f"Source checksum mismatch: {document['id']}")
    text = source.decode("utf-8")
    if document["classification"] == "synthetic":
        if "SYNTHETIC TRAINING DOCUMENT" not in text[:1000]:
            raise ValueError(f"Synthetic source is not clearly labelled: {document['id']}")
        if not Path(document["artifact_path"]).name.startswith("synthetic-"):
            raise ValueError("Synthetic artifact filenames must visibly identify their class")
    elif document["license_notice"] not in text:
        raise ValueError(f"Public source must retain its verified usage notice: {document['id']}")
    return render_pdf(source, document["title"]) if document["format"] == "pdf" else source


def totals(documents: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "document_count": len(documents),
        "page_count": sum(document["page_count"] for document in documents),
        "pdf_documents": sum(document["format"] == "pdf" for document in documents),
        "pdf_pages": sum(
            document["page_count"] for document in documents if document["format"] == "pdf"
        ),
        "markdown_documents": sum(document["format"] == "markdown" for document in documents),
        "markdown_pages": sum(
            document["page_count"] for document in documents if document["format"] == "markdown"
        ),
        "public_documents": sum(document["classification"] == "public" for document in documents),
        "synthetic_documents": sum(
            document["classification"] == "synthetic" for document in documents
        ),
    }


def verify_source_inventory(manifest: dict[str, Any], corpus_root: Path) -> None:
    expected = {document["source_path"] for document in manifest["documents"]}
    found = {
        path.relative_to(corpus_root).as_posix()
        for path in (corpus_root / "sources").rglob("*")
        if path.is_file()
    }
    if found != expected:
        raise ValueError("Source directory has missing or unlisted corpus files")


def verify(manifest: dict[str, Any], corpus_root: Path, directory: Path) -> dict[str, Any]:
    verify_source_inventory(manifest, corpus_root)
    expected = {document["artifact_path"] for document in manifest["documents"]}
    found = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.suffix in {".pdf", ".md"}
    }
    if found != expected:
        raise ValueError("Built corpus has missing or unexpected PDF/Markdown files")
    measured = []
    for document in manifest["documents"]:
        # Validate the authoring source as well as the artifact actually ingested.
        expected_bytes = artifact_bytes(document, corpus_root)
        actual = safe_path(directory, document["artifact_path"]).read_bytes()
        if actual != expected_bytes:
            raise ValueError(f"Artifact differs from its reproducible source: {document['id']}")
        measurement = measure(actual, document["format"])
        if any(document.get(key) != value for key, value in measurement.items()):
            raise ValueError(f"Measured artifact differs from manifest: {document['id']}")
        measured.append({**document, **measurement})
    summary = totals(measured)
    if summary["document_count"] < MIN_DOCUMENTS or summary["page_count"] < MIN_PAGES:
        raise ValueError("Actual corpus files do not meet the 30-document / 150-page floor")
    if not summary["pdf_documents"] or not summary["markdown_documents"]:
        raise ValueError("The corpus must exercise both PDF and Markdown ingestion")
    if summary != manifest.get("totals"):
        raise ValueError("Actual corpus totals do not match the manifest")
    return {"corpus_version": manifest["corpus_version"], **summary}


def build(manifest: dict[str, Any], corpus_root: Path, directory: Path) -> dict[str, Any]:
    # Validate and render every source before writing any build output.
    verify_source_inventory(manifest, corpus_root)
    rendered = [
        (document, artifact_bytes(document, corpus_root)) for document in manifest["documents"]
    ]
    for document, data in rendered:
        measurement = measure(data, document["format"])
        if any(document.get(key) != value for key, value in measurement.items()):
            raise ValueError(f"Source does not reproduce the pinned artifact: {document['id']}")
        output = safe_path(directory, document["artifact_path"])
        if output.is_relative_to(corpus_root.resolve()):
            raise ValueError("Build output must not overwrite a versioned source")
    directory.mkdir(parents=True, exist_ok=True)
    for document, data in rendered:
        output = safe_path(directory, document["artifact_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    return verify(manifest, corpus_root, directory)


def ingest(
    manifest: dict[str, Any], corpus_root: Path, directory: Path, api_url: str, timeout: float
) -> int:
    verify(manifest, corpus_root, directory)
    versions = sorted({document["ingestion_version"] for document in manifest["documents"]})
    for version in versions:
        paths = [
            str(safe_path(directory, document["artifact_path"]))
            for document in manifest["documents"]
            if document["ingestion_version"] == version
        ]
        command = [
            sys.executable,
            str(ROOT / "scripts/ingest_documents.py"),
            "--api-url",
            api_url,
            "--wait",
            "--timeout",
            str(timeout),
            "--version",
            str(version),
            *paths,
        ]
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "verify", "ingest"])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-directory", type=Path, default=ROOT / ".tasks/corpus-build")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive, finite number")
    try:
        manifest = load_manifest(args.manifest)
        corpus_root = args.manifest.resolve().parent
        if args.command == "verify":
            summary = verify(manifest, corpus_root, args.output_directory)
        else:
            summary = build(manifest, corpus_root, args.output_directory)
        print(json.dumps(summary), flush=True)
        if args.command == "ingest":
            return ingest(manifest, corpus_root, args.output_directory, args.api_url, args.timeout)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Corpus verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
