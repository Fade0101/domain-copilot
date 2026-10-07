from collections.abc import AsyncIterator

from app.application.errors import ProviderRateLimitError, ProviderUnavailableError
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    StreamChunk,
)


class FallbackLLMProvider(ILLMProvider):
    """A composite provider that falls back to a secondary provider on transient errors."""

    def __init__(self, primary: ILLMProvider, secondary: ILLMProvider):
        self._primary = primary
        self._secondary = secondary

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            return await self._primary.complete(request)
        except (ProviderUnavailableError, ProviderRateLimitError):
            # Transient error, exactly preserve the original request for the fallback.
            return await self._secondary.complete(request)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        primary_iter = self._primary.stream(request)
        try:
            try:
                first_chunk = await anext(primary_iter)
            except StopAsyncIteration:
                return
            except (ProviderUnavailableError, ProviderRateLimitError):
                pass
            else:
                yield first_chunk
                # Once output is observable, a failure must propagate. Starting
                # another completion would mix two answers in durable history.
                async for chunk in primary_iter:
                    yield chunk
                return
        finally:
            close = getattr(primary_iter, "aclose", None)
            if close is not None:
                await close()

        secondary_iter = self._secondary.stream(request)
        try:
            async for chunk in secondary_iter:
                yield chunk
        finally:
            close = getattr(secondary_iter, "aclose", None)
            if close is not None:
                await close()

    async def aclose(self) -> None:
        for provider in (self._primary, self._secondary):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
