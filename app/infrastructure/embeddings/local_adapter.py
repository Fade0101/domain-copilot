from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from app.application.errors import ProviderConfigurationError
from app.application.ports.embeddings import EmbeddingResult, IEmbeddingProvider
from app.application.ports.ingestion import TokenizedText

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class LocalEmbeddingAdapter(IEmbeddingProvider):
    """Adapter for local embeddings using sentence-transformers."""

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        model: SentenceTransformer | None = None,
    ) -> None:
        self._model_name = model_name
        self._model = model
        # Using a thread pool since SentenceTransformer inference is synchronous
        # and CPU-bound, which would otherwise block the async event loop.
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            # A Celery process that only imports the composition root must not
            # load the ML stack before it can consume a job.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _generate_sync(self, texts: list[str]) -> list[list[float]]:
        # Compute embeddings; returns a numpy array which we convert to list
        embeddings = self._get_model().encode(texts, convert_to_numpy=True)
        return embeddings.tolist()

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(self._executor, self._generate_sync, texts)

        return EmbeddingResult(
            vectors=vectors,
            model_name=self._model_name,
            dimensions=self._get_model().get_sentence_embedding_dimension() or 0,
        )

    def _tokenize_sync(self, text: str) -> TokenizedText:
        model = self._get_model()
        tokenizer = model.tokenizer
        encoded = tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            return_offsets_mapping=True,
        )
        offsets = [
            (int(start), int(end)) for start, end in encoded["offset_mapping"] if end > start
        ]
        maximum = model.max_seq_length
        if maximum is None:
            raise ProviderConfigurationError("The embedding model must declare its token limit.")
        return TokenizedText(
            offsets=offsets,
            embedding_limit=maximum - tokenizer.num_special_tokens_to_add(pair=False),
        )

    async def tokenize(self, text: str) -> TokenizedText:
        """Expose tokenizer offsets through a plain-data port; model loading stays lazy."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._tokenize_sync, text
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False)
