"""Write retrieval and clinical-tool audit events to the existing #6 trace/span models.

No new tables, pools, tracing port or tracing service. The existing logging
audit sink remains the fallback if trace persistence is temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.clinical_tools.contracts import ToolName
from app.application.ports.audit import AuditEntry, IAuditSink
from app.infrastructure.persistence.models import SpanModel, TraceModel

logger = logging.getLogger("app.audit")
_TOOL_ACTIONS = frozenset("clinical_tool." + name.value for name in ToolName) | {
    "clinical_tool.unknown"
}


class PostgresRetrievalAuditSink(IAuditSink):
    def __init__(self, sessions: async_sessionmaker[AsyncSession], fallback: IAuditSink) -> None:
        self._sessions = sessions
        self._fallback = fallback

    async def record(self, entry: AuditEntry) -> None:
        await self._fallback.record(entry)
        is_tool = entry.action in _TOOL_ACTIONS
        if entry.action not in {"retrieval.hybrid", "qa.ask"} and not is_tool:
            return
        try:
            telemetry = json.loads(entry.detail["telemetry"])
            usage = telemetry.get("usage", {})
            trace_id = UUID(entry.resource_id or "")
            async with asyncio.timeout(2):
                async with self._sessions() as session, session.begin():
                    trace = insert(TraceModel).values(
                        id=trace_id,
                        user_id=UUID(entry.actor_id),
                        correlation_id=entry.correlation_id,
                        start_time=datetime.fromisoformat(entry.detail["started_at"]),
                        end_time=entry.occurred_at,
                    )
                    await session.execute(
                        trace.on_conflict_do_update(
                            index_elements=[TraceModel.id], set_={"end_time": entry.occurred_at}
                        )
                    )
                    session.add(
                        SpanModel(
                            id=uuid4(),
                            trace_id=trace_id,
                            name=entry.action,
                            step_type=(
                                "tool"
                                if is_tool
                                else "retrieval"
                                if entry.action == "retrieval.hybrid"
                                else "llm"
                            ),
                            inputs=(
                                {
                                    "tool": telemetry.get("tool", "unknown"),
                                    "agent_scope": telemetry.get("agent_scope"),
                                    "workflow_id": telemetry.get("workflow_id"),
                                }
                                if is_tool
                                else {"query": entry.detail["query"]}
                            ),
                            outputs={**telemetry, "outcome": entry.outcome},
                            duration=telemetry.get("latency_ms", 0) / 1000,
                            tokens=usage.get("total_tokens"),
                            status="ERROR" if entry.outcome in {"error", "denied"} else "OK",
                        )
                    )
        except Exception:
            # An audit sink must not turn a valid answer into a failed request;
            # the full event was already recorded by the existing logging sink.
            logger.warning("Retrieval trace persistence failed; event retained in audit log")
