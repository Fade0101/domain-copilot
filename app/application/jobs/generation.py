"""Bounded text generation on the existing queue and provider port (#21).

This operation produces model text, never a clinical draft, evidence or approval.
The existing grounded /ask and clinical agents keep their own contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.application.ports.jobs import IJobContext
from app.application.ports.llm import CompletionRequest, ILLMProvider, ModelOptions, StreamChunk
from app.domain.shared.errors import InvariantViolationError

GENERATION_OPERATION = "llm.generate"


class GenerationJobHandler:
    operation_type = GENERATION_OPERATION

    def __init__(
        self, provider: Callable[[], ILLMProvider], *, timeout_seconds: float = 60
    ) -> None:
        self._provider = provider
        self._timeout = timeout_seconds

    def validate(self, payload: dict[str, Any]) -> None:
        if set(payload) - {"prompt", "stream", "max_tokens"}:
            raise InvariantViolationError("Unknown text generation input field.")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4_000:
            raise InvariantViolationError("Generation prompt must contain 1 to 4000 characters.")
        if type(payload.get("stream", False)) is not bool:
            raise InvariantViolationError("stream must be a boolean.")
        maximum = payload.get("max_tokens", 1024)
        if type(maximum) is not int or not 1 <= maximum <= 2048:
            raise InvariantViolationError("max_tokens must be between 1 and 2048.")

    async def run(self, context: IJobContext) -> dict[str, Any]:
        self.validate(context.payload)

        async def generate() -> dict[str, Any]:
            provider = self._provider()
            request = CompletionRequest(
                messages=[{"role": "user", "content": context.payload["prompt"]}],
                model_options=ModelOptions(
                    max_tokens=context.payload.get("max_tokens", 1024),
                    timeout_seconds=self._timeout,
                ),
            )
            try:
                async with asyncio.timeout(self._timeout):
                    if context.streaming:
                        return await self._stream(provider, request, context)
                    response = await provider.complete(request)
                    if (
                        response.tool_calls
                        or not response.content
                        or len(response.content) > 65_536
                    ):
                        raise InvariantViolationError("Provider did not return bounded text.")
                    return {"text": response.content, "usage": response.usage or {}}
            finally:
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()

        return await context.step("generation-v1", generate)

    async def _stream(
        self, provider: ILLMProvider, request: CompletionRequest, context: IJobContext
    ) -> dict[str, Any]:
        stream = provider.stream(request)
        pending: asyncio.Future[StreamChunk] | None = None
        parts: list[str] = []
        usage: dict[str, int] = {}
        length = 0
        chunks = 0
        finish_reason = None
        try:
            while True:
                await context.check_cancelled()
                pending = asyncio.ensure_future(anext(stream))
                # A quiet provider must not hide a cancellation until its next
                # token. Only provider I/O is cancelled, never the worker process.
                while not pending.done():
                    await asyncio.wait({pending}, timeout=0.25)
                    await context.check_cancelled()
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    break
                chunks += 1
                if chunks > 16_384 or chunk.tool_calls:
                    raise InvariantViolationError("Invalid generation stream.")
                if chunk.delta:
                    if finish_reason is not None:
                        raise InvariantViolationError("Provider emitted text after completion.")
                    length += len(chunk.delta)
                    if length > 65_536:
                        raise InvariantViolationError("Generation output is too large.")
                    await context.emit_token(chunk.delta)
                    parts.append(chunk.delta)
                if chunk.finish_reason is not None:
                    if chunk.finish_reason not in {"stop", "length"}:
                        raise InvariantViolationError("Provider did not complete text generation.")
                    finish_reason = chunk.finish_reason
                if chunk.usage is not None:
                    usage = chunk.usage
            if finish_reason is None or not parts:
                raise InvariantViolationError("Provider stream ended without completed text.")
            return {"text": "".join(parts), "usage": usage, "finish_reason": finish_reason}
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
