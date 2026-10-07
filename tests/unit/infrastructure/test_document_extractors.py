"""Real PDF and CommonMark parsing, including explicit unsupported-source failures."""

from __future__ import annotations

import pytest

from app.application.errors import IngestionError
from app.infrastructure.ingestion.extractors import DocumentExtractor
from tests.support.document_fixtures import synthetic_pdf


@pytest.fixture
def extractor() -> DocumentExtractor:
    return DocumentExtractor(max_pages=10, max_characters=100_000)


class TestExtraction:
    async def test_markdown_preserves_nested_and_setext_headings(
        self, extractor: DocumentExtractor
    ) -> None:
        blocks = await extractor.extract(
            b"# Synthetic guide\n\nOverview\n--------\n\nAlpha **evidence**.\n\n"
            b"### Follow up\n\nBeta notes.\n\n```text\n# literal content\n```",
            "text/markdown",
        )
        alpha = next(block for block in blocks if "Alpha" in block.text)
        assert alpha.headings == ("Synthetic guide", "Overview")
        assert alpha.text == "Alpha evidence." and alpha.page is None
        code = next(block for block in blocks if "literal" in block.text)
        assert code.headings == ("Synthetic guide", "Overview", "Follow up")

    async def test_pdf_keeps_one_based_pages_and_section_titles(
        self, extractor: DocumentExtractor
    ) -> None:
        blocks = await extractor.extract(
            synthetic_pdf(
                ["OVERVIEW\nSynthetic alpha evidence.", "FOLLOW UP\nSynthetic beta evidence."]
            ),
            "application/pdf",
        )
        assert [block.page for block in blocks] == [1, 2]
        assert [block.headings for block in blocks] == [("OVERVIEW",), ("FOLLOW UP",)]
        assert "alpha" in blocks[0].text and "beta" in blocks[1].text

    @pytest.mark.parametrize(
        "source, media_type, message",
        [
            (b"%PDF-broken", "application/pdf", "could not be read"),
            (b"\xff\xfe", "text/markdown", "UTF-8"),
            (b"binary\x00content", "text/markdown", "binary"),
            (b" \n\t", "text/markdown", "no extractable text"),
        ],
    )
    async def test_unreadable_sources_have_safe_reasons(
        self, extractor: DocumentExtractor, source: bytes, media_type: str, message: str
    ) -> None:
        with pytest.raises(IngestionError, match=message):
            await extractor.extract(source, media_type)

    async def test_scanned_or_encrypted_pdf_has_an_explicit_failure(
        self, extractor: DocumentExtractor
    ) -> None:
        with pytest.raises(IngestionError, match="OCR"):
            await extractor.extract(synthetic_pdf([""]), "application/pdf")
        with pytest.raises(IngestionError, match="Encrypted"):
            await extractor.extract(synthetic_pdf(["SYNTHETIC"], encrypted=True), "application/pdf")

    async def test_limits_reject_oversized_extraction(self) -> None:
        extractor = DocumentExtractor(max_pages=1, max_characters=5)
        with pytest.raises(IngestionError, match="page limit"):
            await extractor.extract(synthetic_pdf(["one", "two"]), "application/pdf")
        with pytest.raises(IngestionError, match="text limit"):
            await extractor.extract(b"synthetic content", "text/markdown")
