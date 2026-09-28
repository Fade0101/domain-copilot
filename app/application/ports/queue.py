"""Port: asynchronous job queue (BRD T7; SDD: Celery + Redis broker).

STUB -- final contract in the async-jobs ticket (#20). SDK-free: no Celery or
Redis type appears here, so application code stays broker-agnostic and the T7
job lifecycle is owned by the domain/application layers, not the transport.
"""

from __future__ import annotations

from typing import Any, Protocol


class IJobQueue(Protocol):
    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue a job and return its job id. Idempotent on ``idempotency_key``."""
        ...
