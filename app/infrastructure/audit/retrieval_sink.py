"""Persist execution audit events on the existing #6 trace/span/cost tables.

The existing logging sink retains every audit event if PostgreSQL is unavailable.
Only provider spans write accounting entries; enclosing asks/agents do not double count.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.clinical_tools.contracts import ToolName
from app.application.observability.context import current_trace
from app.application.ports.audit import AuditEntry, IAuditSink
from app.domain.observability.entities import CostEntry, Span, SpanStatus, Trace
from app.domain.observability.pricing import CostEstimator
from app.infrastructure.persistence.sql.trace_store import PostgresTraceStore

logger = logging.getLogger("app.audit")
_TOOL_ACTIONS = frozenset("clinical_tool." + name.value for name in ToolName) | {
    "clinical_tool.unknown"
}
_OBSERVATION_ACTIONS = {"observability.started", "observability.span"}


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_147_483_647 else None


class PostgresRetrievalAuditSink(IAuditSink):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        fallback: IAuditSink,
        estimator: CostEstimator | None = None,
    ) -> None:
        self._store = PostgresTraceStore(sessions)
        self._fallback = fallback
        self._estimator = estimator or CostEstimator()

    def _cost(self, trace: Trace, span: Span) -> CostEntry | None:
        if span.step_type not in {"llm", "embedding"}:
            return None
        data = span.outputs
        provider, model = str(data["provider"]), str(data["model"])
        usage = data.get("usage") or {}
        prompt = _count(usage.get("prompt_tokens"))
        completion = _count(usage.get("completion_tokens"))
        total = _count(usage.get("total_tokens"))
        reported = prompt is not None and completion is not None
        if reported:
            assert prompt is not None and completion is not None
            if total is not None and total != prompt + completion:
                # Inconsistent provider counters are not a trustworthy billing basis.
                reported = False
            else:
                total = prompt + completion
        if data.get("streaming") and not data.get("stream_complete"):
            reported = False
        cost = self._estimator.estimate(provider, model, prompt, completion) if reported else None
        rate = self._estimator.rate(provider, model)
        return CostEntry(
            id=span.id,
            trace_id=trace.id,
            span_id=span.id,
            job_id=trace.job_id,
            provider=provider,
            model=model,
            tokens_prompt=prompt,
            tokens_completion=completion,
            total_tokens=total,
            usage_status="provider_reported" if reported else "unavailable",
            cost=cost,
            cost_status="estimated" if cost is not None else "unavailable",
            created_at=span.end_time or trace.start_time,
            rate_source=rate.source if rate else None,
            prompt_rate_per_million=rate.prompt_per_million if rate else None,
            completion_rate_per_million=rate.completion_per_million if rate else None,
        )

    async def record(self, entry: AuditEntry) -> None:
        await self._fallback.record(entry)
        is_tool = entry.action in _TOOL_ACTIONS
        if (
            entry.action not in {"retrieval.hybrid", "qa.ask"} | _OBSERVATION_ACTIONS
            and not is_tool
        ):
            return
        try:
            context = current_trace()
            started_at = datetime.fromisoformat(entry.detail["started_at"])
            trace_id = UUID(entry.resource_id or "")
            start_only = entry.action == "observability.started"
            generic = entry.action == "observability.span"
            completes = (
                entry.detail.get("trace_complete") == "true"
                if generic
                else not start_only
                and (
                    is_tool
                    or entry.action == "qa.ask"
                    or context is None
                    or context.kind == "retrieval"
                )
            )
            run_id = entry.detail.get("run_id")
            job_id = entry.detail.get("job_id")
            trace = Trace(
                id=trace_id,
                user_id=UUID(entry.actor_id),
                correlation_id=entry.correlation_id,
                run_id=UUID(run_id) if run_id else context.run_id if context else trace_id,
                job_id=(UUID(job_id) if job_id else None)
                if generic or start_only
                else context.job_id
                if context
                else None,
                start_time=started_at,
                end_time=entry.occurred_at if completes else None,
            )
            span = None
            cost = None
            if not start_only:
                telemetry = json.loads(entry.detail["telemetry"])
                span = Span(
                    id=UUID(entry.detail["span_id"]) if generic else uuid4(),
                    trace_id=trace_id,
                    name=entry.detail["name"] if generic else entry.action,
                    step_type=(
                        entry.detail["step_type"]
                        if generic
                        else "tool"
                        if is_tool
                        else "retrieval"
                        if entry.action == "retrieval.hybrid"
                        else "request"
                    ),
                    inputs=(
                        {}
                        if generic
                        else {
                            "tool": telemetry.get("tool", "unknown"),
                            "agent_scope": telemetry.get("agent_scope"),
                            "workflow_id": telemetry.get("workflow_id"),
                        }
                        if is_tool
                        else {"query": entry.detail["query"]}
                    ),
                    outputs={**telemetry, "outcome": entry.outcome},
                    duration=telemetry.get("latency_ms", 0) / 1000,
                    tokens=_count((telemetry.get("usage") or {}).get("total_tokens")),
                    status=SpanStatus.ERROR
                    if entry.outcome in {"error", "denied"}
                    else SpanStatus.OK,
                    start_time=started_at,
                    end_time=entry.occurred_at,
                )
                cost = self._cost(trace, span)
                if cost is not None:
                    # Persist rate provenance on the span as well as the authoritative ledger.
                    span.outputs["accounting"] = {
                        "usage_status": cost.usage_status,
                        "cost": cost.cost,
                        "cost_status": cost.cost_status,
                        "currency": "USD",
                        "is_estimate": True,
                        "rate_source": cost.rate_source,
                    }
            async with asyncio.timeout(2):
                await self._store.record(trace, span, cost)
        except Exception as exc:
            logger.warning(
                "Trace persistence failed; event retained in audit log (%s)", type(exc).__name__
            )
