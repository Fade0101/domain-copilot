"""Aggregate the durable ledger, never re-estimate historical calls from spans."""

from app.application.auth.context import Principal
from app.application.observability.trace_service import TraceService
from app.domain.observability.entities import TraceQuery, UsageCostReport


class CostService:
    def __init__(self, traces: TraceService) -> None:
        self._traces = traces

    async def report(self, principal: Principal, query: TraceQuery) -> UsageCostReport:
        scoped = await self._traces.scoped_query(principal, query, costs=True)
        rows = await self._traces.store.usage(scoped)
        calls = sum(row.calls for row in rows)
        cost_complete = calls > 0 and all(row.unavailable_cost_calls == 0 for row in rows)
        known_cost = sum(row.known_estimated_cost for row in rows)
        return UsageCostReport(
            calls=calls,
            tokens_prompt=sum(row.tokens_prompt for row in rows),
            tokens_completion=sum(row.tokens_completion for row in rows),
            total_tokens=sum(row.total_tokens for row in rows),
            usage_complete=calls > 0 and all(row.unavailable_usage_calls == 0 for row in rows),
            cost_complete=cost_complete,
            known_estimated_cost=known_cost,
            estimated_cost=known_cost if cost_complete else None,
            cost_status="estimated" if cost_complete else "unavailable",
            breakdown=tuple(rows),
        )
