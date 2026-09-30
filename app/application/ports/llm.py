"""Port: chat / completion LLM provider (BRD AR-2a).

Defines the provider-neutral DTOs and ILLMProvider interface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolDefinition:
    """Definition of a tool available to the provider."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    """A tool call requested by the provider."""

    id: str
    name: str
    arguments: str  # JSON payload representing arguments


@dataclass
class ToolResult:
    """Result of a tool executed by the application."""

    tool_call_id: str
    output: str


@dataclass
class ModelOptions:
    """Provider-neutral model options."""

    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None


@dataclass
class CompletionRequest:
    """Provider-neutral request for a chat completion."""

    messages: list[dict[str, Any]]
    model_options: ModelOptions | None = None
    tools: list[ToolDefinition] | None = None


@dataclass
class CompletionResponse:
    """Provider-neutral response from a chat completion."""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: dict[str, int] | None = None


@dataclass
class StreamChunk:
    """A single chunk from a streaming completion."""

    delta: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@runtime_checkable
class ILLMProvider(Protocol):
    """Abstract interface for LLM providers."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Return a single completion for the given request."""
        ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Yield completion chunks as they are produced (SSE streaming)."""
        ...
