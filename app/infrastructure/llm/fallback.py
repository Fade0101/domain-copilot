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
        try:
            # We must buffer the first chunk or iterator to catch connection/auth errors
            # before we yield anything. If the initial connection fails with a transient
            # error, we can fallback.
            primary_iter = self._primary.stream(request)

            # Use anext to grab the first item, allowing us to catch exceptions.
            # We catch StopAsyncIteration in case the stream is empty, which is fine.
            first_chunk = await anext(primary_iter)
            yield first_chunk

            async for chunk in primary_iter:
                yield chunk

            return

        except StopAsyncIteration:
            return
        except (ProviderUnavailableError, ProviderRateLimitError):
            # Fallback to secondary if primary fails immediately
            pass

        # If we reached here, primary failed with a transient error BEFORE yielding chunks
        async for chunk in self._secondary.stream(request):
            yield chunk
