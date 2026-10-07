"""Task-local observation identity. Context managers always restore their caller."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4

_correlation: ContextVar[str | None] = ContextVar("correlation_id", default=None)


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: UUID
    user_id: UUID
    correlation_id: str
    run_id: UUID
    job_id: UUID | None = None
    kind: str = "request"


_trace: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def get_current_correlation_id() -> str | None:
    return _correlation.get()


def current_trace() -> TraceContext | None:
    return _trace.get()


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[None]:
    token = _correlation.set(correlation_id)
    try:
        yield
    finally:
        _correlation.reset(token)


@contextmanager
def trace_scope(
    trace_id: UUID,
    user_id: UUID,
    *,
    correlation_id: str | None = None,
    run_id: UUID | None = None,
    job_id: UUID | None = None,
    kind: str = "request",
) -> Iterator[TraceContext]:
    parent = current_trace()
    context = TraceContext(
        trace_id=trace_id,
        user_id=user_id,
        correlation_id=correlation_id or get_current_correlation_id() or str(uuid4()),
        run_id=run_id or (parent.run_id if parent else trace_id),
        job_id=job_id or (parent.job_id if parent else None),
        kind=kind,
    )
    with correlation_scope(context.correlation_id):
        token = _trace.set(context)
        try:
            yield context
        finally:
            _trace.reset(token)
