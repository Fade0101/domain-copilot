"""Observe the real AskUseCase via its existing audit port, without a second search."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict
from time import perf_counter
from typing import Any

from app.application.auth.context import Principal
from app.application.ports.audit import AuditEntry, IAuditSink
from app.application.qa.use_cases import AskUseCase


class EvaluationAuditCapture(IAuditSink):
    def __init__(self, delegate: IAuditSink) -> None:
        self._delegate = delegate
        self.entries: ContextVar[list[AuditEntry] | None] = ContextVar(
            "evaluation_audit_entries", default=None
        )

    async def record(self, entry: AuditEntry) -> None:
        entries = self.entries.get()
        if entries is not None:
            entries.append(entry)
        await self._delegate.record(entry)


class EvaluationProbe:
    def __init__(
        self,
        ask: AskUseCase,
        capture: EvaluationAuditCapture,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._ask = ask
        self._capture = capture
        self._close = close

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def execute(self, query: str, principal: Principal) -> dict[str, Any]:
        entries: list[AuditEntry] = []
        token = self._capture.entries.set(entries)
        started = perf_counter()
        result: dict[str, Any] = {"answer": None, "citations": [], "refused": None}
        try:
            answer = await self._ask.execute(query, principal)
            result.update(
                answer=answer.answer,
                citations=[
                    {
                        **asdict(citation),
                        "document_id": str(citation.document_id),
                        "chunk_id": str(citation.chunk_id),
                    }
                    for citation in answer.citations
                ],
                refused=answer.refused,
                trace_id=answer.trace_id,
            )
        except Exception as exc:
            # Every case is attempted. Never serialize provider/driver messages,
            # which can contain credentials or unrelated request content.
            result["error"] = type(exc).__name__
        finally:
            self._capture.entries.reset(token)
        events = {
            entry.action: {
                **json.loads(entry.detail["telemetry"]),
                "outcome": entry.outcome,
                "trace_id": entry.resource_id,
            }
            for entry in entries
            if entry.action in {"retrieval.hybrid", "qa.ask"}
        }
        result["retrieval"] = events.get("retrieval.hybrid", {})
        result["generation"] = events.get("qa.ask", {})
        result["latency_ms"] = (perf_counter() - started) * 1000
        if result.get("error"):
            result["error_phase"] = (
                "generation"
                if result["retrieval"].get("outcome") in {"completed", "refused"}
                else "retrieval"
            )
        return result
