"""Local embedding calls have model attribution, with unavailable token/compute cost."""

from app.application.observability.recording import observe
from app.application.ports.embeddings import EmbeddingResult, IEmbeddingProvider
from app.application.retrieval.observability import RetrievalObserver


class ObservedEmbeddingProvider:
    def __init__(
        self, wrapped: IEmbeddingProvider, observer: RetrievalObserver, *, provider: str, model: str
    ) -> None:
        self._wrapped = wrapped
        self._observer = observer
        self._provider = provider
        self._model = model

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        async with observe(self._observer, "embedding.generate", "embedding") as data:
            data.update(provider=self._provider, model=self._model, input_count=len(texts))
            result = await self._wrapped.generate_embeddings(texts)
            data.update(model=result.model_name, dimensions=result.dimensions)
            return result
