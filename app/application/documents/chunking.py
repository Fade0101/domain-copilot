"""Structure-aware, token-bounded windows over pages and heading hierarchies (#8)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

from app.application.errors import IngestionError
from app.application.ports.ingestion import ITextTokenizer
from app.domain.documents.ingestion import (
    IngestionChunk,
    IngestionOptions,
    TextBlock,
    chunk_identity,
)


def clean_blocks(blocks: list[TextBlock]) -> list[TextBlock]:
    """Normalize presentation whitespace without changing doses, symbols or wording."""
    cleaned = []
    for block in blocks:
        text = unicodedata.normalize("NFC", block.text.replace("\r\n", "\n").replace("\r", "\n"))
        text = "".join(
            char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc"
        )
        text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            cleaned.append(replace(block, text=text))
    if not cleaned:
        raise IngestionError("The document contains no indexable text after cleaning.")
    return cleaned


async def token_windows(
    text: str,
    tokenizer: ITextTokenizer,
    limit: int,
    overlap: int,
) -> AsyncIterator[tuple[str, int]]:
    """Use tokenizer offsets so fragments keep source spelling and Unicode intact."""
    offsets = (await tokenizer.tokenize(text)).offsets
    start = 0
    while start < len(offsets):
        end = min(start + limit, len(offsets))
        while True:
            fragment = text[offsets[start][0] : offsets[end - 1][1]]
            count = len((await tokenizer.tokenize(fragment)).offsets)
            if count <= limit:
                break
            # A subword at a new boundary can tokenize differently. Validate the
            # actual fragment, rather than assuming original offsets prove its size.
            end -= max(1, count - limit)
            if end <= start:
                raise IngestionError("A source token cannot fit the configured token window.")
        yield fragment, count
        if end == len(offsets):
            break
        start = max(start + 1, end - overlap)


class StructureAwareChunker:
    def __init__(self, tokenizer: ITextTokenizer) -> None:
        self._tokenizer = tokenizer

    async def chunk(
        self,
        document_id: UUID,
        blocks: list[TextBlock],
        options: IngestionOptions,
    ) -> list[IngestionChunk]:
        # Adjacent paragraphs can share a window; no overlap crosses a page or heading.
        groups: list[TextBlock] = []
        for block in blocks:
            if groups and (groups[-1].page, groups[-1].headings) == (block.page, block.headings):
                groups[-1] = replace(groups[-1], text=groups[-1].text + "\n\n" + block.text)
            else:
                groups.append(block)
        chunks: list[IngestionChunk] = []
        for block in groups:
            async for text, count in token_windows(
                block.text,
                self._tokenizer,
                options.chunk_tokens,
                options.chunk_overlap,
            ):
                chunks.append(
                    IngestionChunk(
                        id=chunk_identity(document_id, options, len(chunks), block, text),
                        text=text,
                        section=" > ".join(block.headings) or None,
                        page=block.page,
                        token_count=count,
                    )
                )
                if len(chunks) > options.max_chunks:
                    raise IngestionError("The document exceeds the configured chunk limit.")
        if not chunks:
            raise IngestionError("The document contains no tokenizable text.")
        return chunks
