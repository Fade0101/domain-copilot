"""Port: dispatch a durable job ID, never broker-owned job state (BRD T7)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class IJobQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None:
        """Publish an existing PostgreSQL job; raise JobQueueUnavailableError on failure."""
        ...
