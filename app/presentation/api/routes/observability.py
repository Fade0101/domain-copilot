"""Authorized trace and usage queries, using the existing identity and error contracts."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.application.auth.context import Principal
from app.application.observability.cost_service import CostService
from app.application.observability.trace_service import TraceService
from app.domain.observability.entities import TraceQuery
from app.presentation.api.dependencies import get_cost_service, get_trace_service
from app.presentation.api.schemas.observability import (
    SpanPage,
    SpanResponse,
    TracePage,
    TraceResponse,
    UsageResponse,
)
from app.presentation.api.security import get_current_principal

router = APIRouter(
    tags=["observability"],
    responses={
        401: {"description": "Missing, invalid or expired bearer token"},
        403: {"description": "The caller cannot access this user's observations"},
        503: {"description": "Trace storage is unavailable"},
    },
)


def trace_query(
    run_id: UUID | None = None,
    job_id: UUID | None = None,
    correlation_id: str | None = Query(default=None, max_length=255),
    user_id: UUID | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TraceQuery:
    return TraceQuery(
        run_id=run_id,
        job_id=job_id,
        correlation_id=correlation_id,
        user_id=user_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@router.get("/traces", response_model=TracePage)
async def query_traces(
    response: Response,
    principal: Principal = Depends(get_current_principal),
    query: TraceQuery = Depends(trace_query),
    service: TraceService = Depends(get_trace_service),
) -> TracePage:
    response.headers["Cache-Control"] = "no-store"
    traces = await service.query(principal, query)
    return TracePage(
        items=[TraceResponse.model_validate(trace) for trace in traces],
        limit=query.limit,
        offset=query.offset,
    )


@router.get("/traces/{trace_id}/spans", response_model=SpanPage)
async def trace_spans(
    trace_id: UUID,
    response: Response,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    service: TraceService = Depends(get_trace_service),
) -> SpanPage:
    response.headers["Cache-Control"] = "no-store"
    trace, spans = await service.spans(principal, trace_id, limit=limit, offset=offset)
    return SpanPage(
        trace=TraceResponse.model_validate(trace),
        spans=[SpanResponse.model_validate(span) for span in spans],
        limit=limit,
        offset=offset,
    )


@router.get("/usage", response_model=UsageResponse)
async def usage(
    response: Response,
    principal: Principal = Depends(get_current_principal),
    query: TraceQuery = Depends(trace_query),
    service: CostService = Depends(get_cost_service),
) -> UsageResponse:
    """Totals cover ALL matching ledger entries, independently of trace pagination."""
    response.headers["Cache-Control"] = "no-store"
    return UsageResponse.model_validate(await service.report(principal, query))
