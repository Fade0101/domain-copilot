"""Trace query projections; amounts explicitly distinguish estimates from unavailable costs."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TraceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    run_id: UUID | None
    job_id: UUID | None
    correlation_id: str | None
    start_time: datetime
    end_time: datetime | None


class SpanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trace_id: UUID
    name: str
    step_type: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    status: str
    tokens: int | None
    duration: float | None
    start_time: datetime | None
    end_time: datetime | None


class TracePage(BaseModel):
    items: list[TraceResponse]
    limit: int
    offset: int


class SpanPage(BaseModel):
    trace: TraceResponse
    spans: list[SpanResponse]
    limit: int
    offset: int


class UsageBreakdownResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str
    calls: int
    tokens_prompt: int
    tokens_completion: int
    total_tokens: int
    unavailable_usage_calls: int
    unavailable_cost_calls: int
    known_estimated_cost: float
    estimated_cost: float | None


class UsageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    calls: int
    tokens_prompt: int
    tokens_completion: int
    total_tokens: int
    usage_complete: bool
    cost_complete: bool
    known_estimated_cost: float
    estimated_cost: float | None
    breakdown: list[UsageBreakdownResponse]
    currency: str
    cost_status: str
    is_estimate: bool
    notice: str
