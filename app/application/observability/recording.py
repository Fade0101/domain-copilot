"""Execution spans travel through the existing audit sink, with no prompt bodies."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.application.observability.context import TraceContext, current_trace
from app.application.ports.audit import AuditEntry
from app.application.retrieval.observability import RetrievalObserver


async def _record(
    observer: RetrievalObserver,
    action: str,
    started_at: datetime,
    detail: dict[str, str],
    outcome: str,
    context: TraceContext | None = None,
) -> None:
    context = context or current_trace()
    if context is None:
        return
    await observer.sink.record(
        AuditEntry(
            actor_id=str(context.user_id),
            actor_role="execution",
            action=action,
            outcome=outcome,
            occurred_at=observer.clock.now(),
            resource_type="trace",
            resource_id=str(context.trace_id),
            correlation_id=context.correlation_id,
            detail={
                "started_at": started_at.isoformat(),
                "run_id": str(context.run_id),
                "job_id": str(context.job_id) if context.job_id else "",
                **detail,
            },
        )
    )


async def start_trace(observer: RetrievalObserver | None) -> None:
    if observer is not None:
        await _record(observer, "observability.started", observer.clock.now(), {}, "started")


@asynccontextmanager
async def observe(
    observer: RetrievalObserver | None,
    name: str,
    step_type: str,
    *,
    root: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    outputs: dict[str, Any] = {}
    context = current_trace()
    if observer is None or context is None:
        yield outputs
        return
    started_at = observer.clock.now()
    started = perf_counter()
    span_id = str(uuid4())
    if root:
        await start_trace(observer)
    outcome = "completed"
    try:
        yield outputs
    except BaseException as exc:
        outcome = "error"
        # Includes task cancellation and abandoned streams, without exception text.
        outputs["error_type"] = type(exc).__name__
        raise
    finally:
        outputs["latency_ms"] = (perf_counter() - started) * 1000
        await _record(
            observer,
            "observability.span",
            started_at,
            {
                "name": name,
                "step_type": step_type,
                "span_id": span_id,
                "trace_complete": str(root).lower(),
                "telemetry": json.dumps(outputs, allow_nan=False, separators=(",", ":")),
            },
            str(outputs.get("outcome", outcome)),
            context,
        )
