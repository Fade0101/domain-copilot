"""Retrieval events use the existing audit port and request identity envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from app.application.auth.context import Principal
from app.application.ports.audit import AuditEntry, IAuditSink
from app.application.ports.system import IClock


@dataclass(frozen=True, slots=True)
class RetrievalObserver:
    sink: IAuditSink
    clock: IClock

    async def record(
        self,
        *,
        action: str,
        principal: Principal,
        trace_id: str,
        query: str,
        started_at: datetime,
        telemetry: dict[str, object],
        outcome: str,
    ) -> None:
        await self.sink.record(
            AuditEntry(
                actor_id=principal.user_id.value,
                actor_role=principal.role.value,
                action=action,
                outcome=outcome,
                occurred_at=self.clock.now(),
                resource_type="trace",
                resource_id=trace_id,
                correlation_id=trace_id,
                detail={
                    "query": query,
                    "started_at": started_at.isoformat(),
                    "telemetry": json.dumps(telemetry, allow_nan=False, separators=(",", ":")),
                },
            )
        )
