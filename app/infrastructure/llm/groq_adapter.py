from collections.abc import AsyncIterator
from typing import Any

from groq import APIConnectionError, APIStatusError, AsyncGroq, RateLimitError

from app.application.errors import (
    ContextWindowExceededError,
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    StreamChunk,
    ToolCall,
)


class GroqAdapter(ILLMProvider):
    """Adapter for the Groq LLM using the official groq SDK."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "llama-3.3-70b-versatile",
        client: AsyncGroq | None = None,
    ):
        self._client = client or AsyncGroq(api_key=api_key)
        self._default_model = default_model

    async def aclose(self) -> None:
        """Release a worker job's HTTP pool before its asyncio event loop closes."""
        await self._client.close()

    def _map_error(self, exc: Exception) -> Exception:
        """Map Groq specific exceptions to Application Provider errors."""
        if isinstance(exc, APIConnectionError):
            return ProviderUnavailableError("Groq API is unreachable.")
        if isinstance(exc, RateLimitError):
            return ProviderRateLimitError("Groq API rate limit exceeded.")
        if isinstance(exc, APIStatusError):
            if exc.status_code == 401 or exc.status_code == 403:
                return ProviderAuthenticationError("Groq API authentication failed.")
            if exc.status_code == 400:
                if "context length" in str(exc).lower() or "too many tokens" in str(exc).lower():
                    return ContextWindowExceededError("Context window exceeded for Groq.")
                return ProviderInvalidRequestError(f"Invalid request to Groq: {exc}")
        return exc

    def _prepare_kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._default_model,
            "messages": request.messages,
        }
        if request.model_options:
            if request.model_options.temperature is not None:
                kwargs["temperature"] = request.model_options.temperature
            if request.model_options.max_tokens is not None:
                kwargs["max_tokens"] = request.model_options.max_tokens

        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        return kwargs

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        kwargs = self._prepare_kwargs(request)
        try:
            # We don't map timeout_seconds natively to groq client init per request,
            # but if we wanted to, we could use the timeout param.
            if request.model_options and request.model_options.timeout_seconds is not None:
                kwargs["timeout"] = request.model_options.timeout_seconds

            response = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise self._map_error(e) from e

        choice = response.choices[0]
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                for tc in choice.message.tool_calls
            ]

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return CompletionResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            usage=usage,
            model=getattr(response, "model", self._default_model),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        kwargs = self._prepare_kwargs(request)
        kwargs["stream"] = True

        try:
            if request.model_options and request.model_options.timeout_seconds is not None:
                kwargs["timeout"] = request.model_options.timeout_seconds

            response_stream = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise self._map_error(e) from e

        try:
            async for chunk in response_stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None
                tool_calls = None
                if delta and delta.tool_calls:
                    tool_calls = [
                        ToolCall(
                            id=tc.id or "",
                            name=tc.function.name or "",
                            arguments=tc.function.arguments or "",
                        )
                        for tc in delta.tool_calls
                    ]

                # Groq streaming may return usage in the
                # final chunk via x_groq.
                usage = None
                if chunk.x_groq and chunk.x_groq.get("usage"):
                    usage = chunk.x_groq["usage"]

                yield StreamChunk(
                    delta=delta.content if delta else None,
                    tool_calls=tool_calls,
                    finish_reason=choice.finish_reason if choice else None,
                    usage=usage,
                    model=getattr(chunk, "model", self._default_model),
                )
        except Exception as e:
            raise self._map_error(e) from e
