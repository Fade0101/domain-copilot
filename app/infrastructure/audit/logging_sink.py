"""Logging audit sink (implements :class:`IAuditSink`).

Writes audit entries to the standard logging facility. A durable sink -- the
``approvals`` and ``job_events`` tables, or an append-only store -- belongs to the
tickets that own those records (#17, #19, #23); this adapter makes the identity
that Ticket #5 establishes observable now, so the propagation chain is testable
end to end rather than notional.

Structured key/value fields rather than an interpolated sentence, so a log
processor can index ``actor`` and ``outcome``. :class:`AuditEntry` has no field
for a token, a password, or a hash, so there is nothing here that could emit one.
"""

from __future__ import annotations

import logging

from app.application.ports.audit import AuditEntry, IAuditSink

DEFAULT_AUDIT_LOGGER = "app.audit"


class LoggingAuditSink(IAuditSink):
    """Emits one INFO log record per audit entry."""

    def __init__(self, logger_name: str = DEFAULT_AUDIT_LOGGER) -> None:
        self._logger = logging.getLogger(logger_name)

    async def record(self, entry: AuditEntry) -> None:
        self._logger.info(
            "audit action=%s outcome=%s actor=%s role=%s resource=%s:%s correlation=%s detail=%s",
            entry.action,
            entry.outcome,
            entry.actor_id,
            entry.actor_role,
            entry.resource_type or "-",
            entry.resource_id or "-",
            entry.correlation_id or "-",
            entry.detail or {},
        )
