import asyncio
from concurrent.futures import ThreadPoolExecutor

from sentence_transformers import SentenceTransformer

from app.application.ports.embeddings import EmbeddingResult, IEmbeddingProvider


class LocalEmbeddingAdapter(IEmbeddingProvider):
    """Adapter for local embeddings using sentence-transformers."""

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        model: SentenceTransformer | None = None,
    ):
        self._model_name = model_name
        self._model = model
        # Using a thread pool since SentenceTransformer inference is synchronous
        # and CPU-bound, which would otherwise block the async event loop.
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
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
