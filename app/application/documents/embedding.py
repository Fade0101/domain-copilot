"""Embed all of a chunk even when its 512-token window exceeds a model's context."""

from __future__ import annotations

import math

from app.application.documents.chunking import token_windows
from app.application.errors import IngestionError
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.ingestion import ITextTokenizer
from app.domain.documents.ingestion import IngestionOptions


class IngestionEmbedder:
    def __init__(self, provider: IEmbeddingProvider, tokenizer: ITextTokenizer) -> None:
        self._provider = provider
        self._tokenizer = tokenizer

    async def embed(self, texts: list[str], options: IngestionOptions) -> list[list[float]]:
        fragments: list[str] = []
        owners: list[tuple[int, int]] = []
        for owner, text in enumerate(texts):
            limit = (await self._tokenizer.tokenize(text)).embedding_limit
            if limit < 1:
                raise IngestionError("The embedding model has no usable token window.")
            async for fragment, count in token_windows(text, self._tokenizer, limit, 0):
                fragments.append(fragment)
                owners.append((owner, count))
        sums = [[0.0] * options.embedding_dim for _ in texts]
        weights = [0] * len(texts)
        for start in range(0, len(fragments), options.embedding_batch_size):
            batch = fragments[start : start + options.embedding_batch_size]
            result = await self._provider.generate_embeddings(batch)
            if (
                result.model_name != options.embedding_model
                or result.dimensions != options.embedding_dim
                or len(result.vectors) != len(batch)
            ):
                raise IngestionError(
                    "The embedding provider returned inconsistent provenance or vector counts."
                )
            for offset, vector in enumerate(result.vectors):
                if len(vector) != options.embedding_dim or not all(
                    math.isfinite(v) for v in vector
                ):
                    raise IngestionError("The embedding provider returned an invalid vector.")
                owner, weight = owners[start + offset]
                sums[owner] = [
                    total + value * weight for total, value in zip(sums[owner], vector, strict=True)
                ]
                weights[owner] += weight
        vectors = []
        for total, weight in zip(sums, weights, strict=True):
            norm = math.sqrt(sum(value * value for value in total))
            if not weight or not norm or not math.isfinite(norm):
                raise IngestionError("The embedding provider returned a zero or invalid vector.")
            # Token-weighted mean, normalized for cosine search. Short chunks retain
            # their direction; long chunks include every model-sized window (ADR-001).
            vectors.append([value / norm for value in total])
        return vectors
