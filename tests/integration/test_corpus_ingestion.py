"""Every real corpus artifact is readable by Ticket #8 with its default limits.

No replacement extractors, new formats, network calls or evaluation cases are used.
Full HTTP/Celery/local-model corpus verification is documented in docs/CORPUS.md.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from pypdf import PdfReader

from app.core.config import IngestionSettings
from app.infrastructure.ingestion.extractors import DocumentExtractor
from scripts.corpus import load_manifest, render_pdf
from tests.support.corpus_fixtures import BuiltCorpus
from tests.support.corpus_fixtures import built_corpus as built_corpus

DOCUMENTS = load_manifest()["documents"]


@pytest.mark.parametrize("document", DOCUMENTS, ids=[document["id"] for document in DOCUMENTS])
async def test_actual_corpus_artifact_passes_ticket8_extraction(
    built_corpus: BuiltCorpus, document: dict[str, Any]
) -> None:
    settings = IngestionSettings()
    source = (built_corpus.directory / document["artifact_path"]).read_bytes()
    assert len(source) <= settings.max_upload_bytes
    extractor = DocumentExtractor(
        max_pages=settings.max_pages, max_characters=settings.max_characters
    )
    blocks = await extractor.extract(
        source, "application/pdf" if document["format"] == "pdf" else "text/markdown"
    )
    assert blocks and any(block.headings for block in blocks)
    assert sum(len(block.text) for block in blocks) <= settings.max_characters
    if document["format"] == "pdf":
        assert {block.page for block in blocks} == set(range(1, document["page_count"] + 1))
    else:
        assert all(block.page is None for block in blocks)
    text = "\n".join(block.text for block in blocks)
    if document["classification"] == "synthetic":
        assert "SYNTHETIC TRAINING DOCUMENT" in text
    else:
        assert document["license_notice"] in text
        assert document["source_url"] in text


def test_every_generated_pdf_page_is_textual_and_clearly_synthetic(
    built_corpus: BuiltCorpus,
) -> None:
    physical_pages = 0
    for document in DOCUMENTS:
        if document["format"] != "pdf":
            continue
        data = (built_corpus.directory / document["artifact_path"]).read_bytes()
        pages = PdfReader(BytesIO(data)).pages
        for page in pages:
            text = page.extract_text()
            assert "SYNTHETIC TRAINING DOCUMENT" in text
            assert len(text.split()) >= 20
        physical_pages += len(pages)
    assert physical_pages == 12
    assert physical_pages == built_corpus.summary["pdf_pages"]


def test_pdf_rebuild_is_byte_identical(built_corpus: BuiltCorpus) -> None:
    document = next(document for document in DOCUMENTS if document["format"] == "pdf")
    source = (built_corpus.source_directory / document["source_path"]).read_text(encoding="utf-8")
    assert (
        render_pdf(source.encode(), document["title"])
        == (built_corpus.directory / document["artifact_path"]).read_bytes()
    )
