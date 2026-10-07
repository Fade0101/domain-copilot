import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.application.errors import (
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


def _usage(data: dict[str, Any]) -> dict[str, int] | None:
    counts = {
        target: value
        for target, source in (
            ("prompt_tokens", "prompt_eval_count"),
            ("completion_tokens", "eval_count"),
        )
        if type(value := data.get(source)) is int and value >= 0
    }
    if len(counts) == 2:
        counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
    return counts or None


class OllamaAdapter(ILLMProvider):
    """Adapter for local Ollama LLM via HTTP API."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama3.2",
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._client = client or httpx.AsyncClient(base_url=self._base_url)

    async def aclose(self) -> None:
        """Release a worker job's HTTP pool before its asyncio event loop closes."""
        await self._client.aclose()

    def _map_error(self, exc: Exception) -> Exception:
        """Map httpx specific exceptions to Application Provider errors."""
        if isinstance(exc, httpx.ConnectError | httpx.TimeoutException):
            return ProviderUnavailableError(f"Ollama API is unreachable: {exc}")
        if isinstance(exc, httpx.HTTPStatusError):
            if exc.response.status_code == 429:
                return ProviderRateLimitError("Ollama API rate limit exceeded.")
            if exc.response.status_code == 400:
                return ProviderInvalidRequestError(
                    f"Invalid request to Ollama: {exc.response.text}"
                )
        return exc

    def _prepare_payload(self, request: CompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._default_model,
            "messages": request.messages,
            "stream": False,
        }

        if request.model_options:
            options: dict[str, Any] = {}
            if request.model_options.temperature is not None:
                options["temperature"] = request.model_options.temperature
            if request.model_options.max_tokens is not None:
                options["num_predict"] = request.model_options.max_tokens
            if options:
                payload["options"] = options

        if request.tools:
            payload["tools"] = [
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
        return payload

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = self._prepare_payload(request)
        timeout = 60.0
        if request.model_options and request.model_options.timeout_seconds is not None:
            timeout = request.model_options.timeout_seconds

        url = f"{self._base_url}/api/chat"
        try:
            response = await self._client.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise self._map_error(e) from e

        message = data.get("message", {})
        tool_calls = None
        if "tool_calls" in message:
            tool_calls = []
            for i, tc in enumerate(message["tool_calls"]):
                func = tc.get("function", {})
                tool_calls.append(
                    ToolCall(
                        id=f"call_{i}",
                        name=func.get("name", ""),
                        arguments=json.dumps(func.get("arguments", {})),
                    )
                )

        return CompletionResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=_usage(data),
            model=data.get("model", self._default_model),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        payload = self._prepare_payload(request)
        payload["stream"] = True

        timeout = 60.0
        if request.model_options and request.model_options.timeout_seconds is not None:
            timeout = request.model_options.timeout_seconds

        url = f"{self._base_url}/api/chat"
        try:
            async with self._client.stream(
                "POST",
                url,
                json=payload,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    data = json.loads(line)
                    message = data.get("message", {})

                    # Ollama streaming might return tool calls in a chunk
                    tool_calls = None
                    if "tool_calls" in message:
                        tool_calls = []
                        for i, tc in enumerate(message["tool_calls"]):
                            func = tc.get("function", {})
                            tool_calls.append(
                                ToolCall(
                                    id=f"call_{i}",
                                    name=func.get("name", ""),
                                    arguments=json.dumps(func.get("arguments", {})),
                                )
                            )

                    yield StreamChunk(
                        delta=message.get("content"),
                        tool_calls=tool_calls,
                        finish_reason="stop" if data.get("done") else None,
                        usage=_usage(data) if data.get("done") else None,
                        model=data.get("model", self._default_model),
                    )

        except Exception as e:
            raise self._map_error(e) from e
