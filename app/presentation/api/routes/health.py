"""Liveness/readiness endpoints (SDD A.7: ``/health``, ``/ready``)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness probe: the app is ready to serve requests.

    Downstream dependency checks (database, redis) are added by their tickets.
    """
    return {"status": "ready"}
