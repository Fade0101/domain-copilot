"""Persistence and dependency probes behind framework-free observability ports."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol
from uuid import UUID

from app.domain.observability.entities import (
    CostEntry,
    Span,
    Trace,
    TraceQuery,
    UsageCostBreakdown,
)


class ITraceStore(Protocol):
    async def record(
        self, trace: Trace, span: Span | None = None, cost: CostEntry | None = None
    ) -> None:
        """Persist the trace, span and optional cost in one transaction."""
        ...

    async def get_trace(self, trace_id: UUID) -> Trace | None: ...

    async def get_spans(self, trace_id: UUID, *, limit: int, offset: int) -> list[Span]: ...

    async def query_traces(self, query: TraceQuery) -> list[Trace]: ...

    async def usage(self, query: TraceQuery) -> list[UsageCostBreakdown]:
        """Aggregate all matching ledger entries, independently of trace pagination."""
        ...


class IHealthCheckProvider(Protocol):
    def probes(self) -> Mapping[str, Callable[[], Awaitable[bool]]]: ...
