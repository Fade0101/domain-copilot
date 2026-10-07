"""A failed PostgreSQL trace write preserves the existing audit-log event."""

from datetime import UTC, datetime
from uuid import uuid4

from app.application.ports.audit import AuditEntry
from app.infrastructure.audit.retrieval_sink import PostgresRetrievalAuditSink
from tests.support.fakes import RecordingAuditSink


async def test_database_failure_retains_complete_event_in_existing_audit_sink(caplog) -> None:
    def unavailable():
        raise RuntimeError("private connection details")

    fallback = RecordingAuditSink()
    sink = PostgresRetrievalAuditSink(unavailable, fallback)  # type: ignore[arg-type]
    entry = AuditEntry(
        actor_id=str(uuid4()),
        actor_role="analyst",
        action="retrieval.hybrid",
        outcome="refused",
        occurred_at=datetime.now(UTC),
        resource_id=str(uuid4()),
        detail={
            "query": "synthetic query",
            "telemetry": '{"refused":true}',
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    await sink.record(entry)
    assert fallback.entries == [entry]
    assert "event retained in audit log" in caplog.text
    assert "private connection details" not in caplog.text
