"""Port: chat / completion LLM provider (BRD AR-2a).

STUB -- the signatures here are provisional anchors so downstream code has a
stable import target. The final request/response contracts are defined in the
provider-abstraction ticket (#7). This port is intentionally SDK-free: no
vendor client type appears, so ``app.domain``/``app.application`` never import
an LLM SDK and a provider can be swapped by config + one adapter (BRD AR-1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class ILLMProvider(Protocol):
    async def complete(self, prompt: str, **options: Any) -> str:
        """Return a single completion for ``prompt``."""
        ...

    def stream(self, prompt: str, **options: Any) -> AsyncIterator[str]:
        """Yield completion tokens as they are produced (SSE token streaming)."""
        ...

    async def complete_with_tools(
        self, prompt: str, tools: list[dict[str, Any]], **options: Any
    ) -> Any:
        """Return a completion that may include tool calls. Contract finalized in #7."""
        ...
