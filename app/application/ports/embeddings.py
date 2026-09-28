"""Port: text embedding provider (BRD AR-2b).

STUB -- final contract in the provider-abstraction ticket (#7). SDK-free so the
application never imports ``sentence_transformers`` or a hosted embedding client.
"""

from __future__ import annotations

from typing import Protocol


class IEmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str:
        """Identifier of the embedding model (persisted alongside each embedding)."""
        ...

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this provider produces."""
        ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, preserving input order."""
        ...
