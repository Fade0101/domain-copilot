"""Ingestion records and deterministic identities, independent of storage and parsers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid5

from app.domain.shared.errors import InvariantViolationError


class IngestionStage(StrEnum):
    EXTRACT = "extract"
    CLEAN = "clean"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    page: int | None = None
    headings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionOptions:
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    max_chunks: int = 4096
    embedding_batch_size: int = 32
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    embedding_version: str = "1"
    chunker_version: str = "structure-v1"

    def __post_init__(self) -> None:
        if not 0 <= self.chunk_overlap < self.chunk_tokens:
            raise InvariantViolationError("Chunk overlap must be smaller than the token limit.")
        if self.max_chunks < 1 or self.embedding_batch_size < 1 or self.embedding_dim < 1:
            raise InvariantViolationError("Ingestion batch and dimension limits must be positive.")


@dataclass(frozen=True, slots=True)
class IngestionChunk:
    id: UUID
    text: str
    section: str | None
    page: int | None
    token_count: int


def document_identity(user_id: UUID, digest: str, media_type: str, version: int) -> UUID:
    """An immutable source revision belongs to one uploader; filenames are descriptive."""
    return uuid5(user_id, json.dumps(["document-v1", digest, media_type, version]))


def chunk_identity(
    document_id: UUID, options: IngestionOptions, index: int, block: TextBlock, text: str
) -> UUID:
    """Embedding changes do not change the identity of an unchanged source passage."""
    return uuid5(
        document_id,
        json.dumps(
            [
                options.chunker_version,
                options.chunk_tokens,
                options.chunk_overlap,
                index,
                block.page,
                block.headings,
                hashlib.sha256(text.encode()).hexdigest(),
            ],
            ensure_ascii=False,
        ),
    )
