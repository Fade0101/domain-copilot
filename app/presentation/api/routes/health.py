"""Liveness/readiness endpoints (SDD A.7: ``/health``, ``/ready``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.application.observability.health_service import HealthService
from app.presentation.api.dependencies import get_health_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: the process is up."""
    return {"status": "ok"}


@router.get("/ready", responses={503: {"description": "A dependency is unavailable or timed out"}})
async def ready(service: HealthService = Depends(get_health_service)) -> JSONResponse:
    """Readiness requires PostgreSQL, Redis and each configured provider probe to pass."""
    healthy, payload = await service.check_readiness()
    return JSONResponse(
        payload, status_code=200 if healthy else 503, headers={"Cache-Control": "no-store"}
    )
