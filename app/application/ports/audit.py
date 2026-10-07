"""Audit sink port (BRD FR-8 identity propagation; SDD approval audit trail).

The end of the identity-propagation chain: once a request's principal has been
established and an authorization decision made, that decision is recorded with
the actor's identity attached. Later tickets (#17 observability, #19 approvals,
#23 tracing) write richer entries through this same port.

:class:`AuditEntry` carries an actor *id* and *role* and nothing else about the
credential. It has no field for a token, a password, or a password hash, so an
audit record cannot become the place a secret leaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

#: Outcome values for :attr:`AuditEntry.outcome`.
OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One auditable action attributed to an authenticated principal."""

    actor_id: str
    actor_role: str
    action: str
    outcome: str
    occurred_at: datetime
    resource_type: str | None = None
    resource_id: str | None = None
    correlation_id: str | None = None
    #: Non-sensitive extra context. Callers must not place credentials here.
    detail: dict[str, str] = field(default_factory=dict)


class IAuditSink(Protocol):
    """Durable-ish destination for audit entries."""

    async def record(self, entry: AuditEntry) -> None:
        """Persist ``entry``. Must not raise on a well-formed entry."""
        ...
