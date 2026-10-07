"""Measure individual provider attempts, underneath the existing fallback adapter."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from app.application.observability.recording import observe
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    StreamChunk,
)
from app.application.retrieval.observability import RetrievalObserver


class ObservedLLMProvider:
    def __init__(
        self, wrapped: ILLMProvider, observer: RetrievalObserver, *, provider: str, model: str
    ) -> None:
        self._wrapped = wrapped
        self._observer = observer
        self._provider = provider
        self._model = model

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        async with observe(self._observer, "llm.complete", "llm") as data:
            data.update(provider=self._provider, model=self._model, streaming=False)
            response = await self._wrapped.complete(request)
            data.update(usage=response.usage, model=response.model or self._model)
            return response

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[StreamChunk, None]:
        # Snapshot identity before yielding. Accounting is one final record, never per token.
        async with observe(self._observer, "llm.stream", "llm") as data:
            data.update(provider=self._provider, model=self._model, streaming=True)
            stream = self._wrapped.stream(request)
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        # Adapter usage is cumulative, including a final usage-only chunk.
                        data["usage"] = chunk.usage
                    if chunk.model:
                        data["model"] = chunk.model
                    if chunk.finish_reason:
                        data["finish_reason"] = chunk.finish_reason
                    yield chunk
                data["stream_complete"] = bool(data.get("finish_reason"))
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()

    async def aclose(self) -> None:
        close = getattr(self._wrapped, "aclose", None)
        if close is not None:
            await close()
