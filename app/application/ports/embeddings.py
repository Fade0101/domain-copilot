"""Port: text embedding provider (BRD AR-2b).

Defines the provider-neutral DTOs and IEmbeddingProvider interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class EmbeddingResult:
    """Result of embedding text."""

    vectors: list[list[float]]
    model_name: str
    dimensions: int


@runtime_checkable
class IEmbeddingProvider(Protocol):
    """Abstract interface for text embedding providers."""

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """Generate embeddings for a batch of texts, returning vectors and metadata."""
        ...
