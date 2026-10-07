"""PDF pages/bookmarks and Markdown heading trees, treated only as source data."""

from __future__ import annotations

import asyncio
import re
from io import BytesIO

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pypdf import PdfReader

from app.application.errors import IngestionError
from app.application.ports.ingestion import IDocumentExtractor
from app.domain.documents.ingestion import TextBlock


def _inline(token: Token) -> str:
    if not token.children:
        return token.content
    return "".join(
        "\n" if child.type in {"softbreak", "hardbreak"} else child.content
        for child in token.children
        if child.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
    )


class MarkdownExtractor:
    def extract(self, source: bytes) -> list[TextBlock]:
        try:
            text = source.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise IngestionError("Markdown must be UTF-8 encoded.") from None
        if "\x00" in text:
            raise IngestionError("Markdown contains binary data.")
        # CommonMark recognizes ATX/Setext headings and fences. It never fetches
        # links/images or executes HTML; inline markup becomes readable source text.
        tokens = MarkdownIt("commonmark", {"html": False}).parse(text)
        headings: list[tuple[int, str]] = []
        blocks = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == "heading_open":
                level = int(token.tag[1:])
                title = _inline(tokens[index + 1]).strip()
                headings = [heading for heading in headings if heading[0] < level]
                headings.append((level, title))
                blocks.append(TextBlock(title, headings=tuple(title for _, title in headings)))
                index += 2
            elif token.type in {"inline", "fence", "code_block"}:
                text = _inline(token) if token.type == "inline" else token.content
                if text.strip():
                    blocks.append(TextBlock(text, headings=tuple(title for _, title in headings)))
            index += 1
        return blocks


class PdfExtractor:
    def __init__(self, max_pages: int, max_characters: int) -> None:
        self._max_pages = max_pages
        self._max_characters = max_characters

    def extract(self, source: bytes) -> list[TextBlock]:
        try:
            reader = PdfReader(BytesIO(source))
            if reader.is_encrypted:
                raise IngestionError("Encrypted PDFs are not supported; supply a readable source.")
            if len(reader.pages) > self._max_pages:
                raise IngestionError("The PDF exceeds the configured page limit.")
            outlines: dict[int, tuple[str, ...]] = {}

            def collect(items: list, parents: tuple[str, ...] = ()) -> None:
                previous = parents
                for item in items:
                    if isinstance(item, list):
                        collect(item, previous)
                    else:
                        previous = (*parents, str(item.title))
                        page = reader.get_destination_page_number(item)
                        if page is not None and page >= 0:
                            outlines[page] = previous

            collect(reader.outline)
            blocks = []
            headings: tuple[str, ...] = ()
            characters = 0
            for page_index, page in enumerate(reader.pages):
                headings = outlines.get(page_index, headings)
                text = page.extract_text() or ""
                characters += len(text)
                if characters > self._max_characters:
                    raise IngestionError(
                        "The extracted document exceeds the configured text limit."
                    )
                buffer: list[str] = []
                for line in text.splitlines():
                    stripped = line.strip()
                    # PDF has no universal heading tag. Bookmarks take precedence;
                    # short all-caps or numbered headings provide a conservative fallback.
                    is_heading = 0 < len(stripped) <= 100 and (
                        (stripped.isupper() and any(char.isalpha() for char in stripped))
                        or bool(re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z][A-Za-z ]+$", stripped))
                    )
                    if is_heading:
                        if buffer:
                            blocks.append(TextBlock("\n".join(buffer), page_index + 1, headings))
                            buffer = []
                        if not headings or headings[-1].casefold() != stripped.casefold():
                            headings = (*outlines.get(page_index, ())[:-1], stripped)
                    buffer.append(line)
                if buffer:
                    blocks.append(TextBlock("\n".join(buffer), page_index + 1, headings))
            return blocks
        except IngestionError:
            raise
        except Exception:
            raise IngestionError(
                "The PDF could not be read; supply a valid, text-based PDF."
            ) from None


class DocumentExtractor(IDocumentExtractor):
    def __init__(self, *, max_pages: int, max_characters: int) -> None:
        self._pdf = PdfExtractor(max_pages, max_characters)
        self._markdown = MarkdownExtractor()
        self._max_characters = max_characters

    async def extract(self, source: bytes, media_type: str) -> list[TextBlock]:
        if media_type == "application/pdf":
            blocks = await asyncio.to_thread(self._pdf.extract, source)
        elif media_type == "text/markdown":
            blocks = await asyncio.to_thread(self._markdown.extract, source)
        else:
            raise IngestionError("Only PDF and Markdown sources are supported.")
        if not any(block.text.strip() for block in blocks):
            raise IngestionError("The document contains no extractable text; OCR is not supported.")
        if sum(len(block.text) for block in blocks) > self._max_characters:
            raise IngestionError("The extracted document exceeds the configured text limit.")
        return blocks
