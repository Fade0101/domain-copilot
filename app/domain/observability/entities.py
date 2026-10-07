"""Provider-neutral trace and accounting records (Ticket #23)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class SpanStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class Trace:
    id: UUID
    user_id: UUID
    start_time: datetime
    correlation_id: str | None = None
    run_id: UUID | None = None
    job_id: UUID | None = None
    end_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class Span:
    id: UUID
    trace_id: UUID
    name: str
    step_type: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    status: SpanStatus = SpanStatus.OK
    tokens: int | None = None
    duration: float | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class TraceQuery:
    run_id: UUID | None = None
    job_id: UUID | None = None
    correlation_id: str | None = None
    user_id: UUID | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CostEntry:
    """One physical provider call; missing usage or prices are explicitly unknown."""

    id: UUID
    trace_id: UUID
    span_id: UUID
    provider: str
    model: str
    tokens_prompt: int | None
    tokens_completion: int | None
    total_tokens: int | None
    usage_status: str
    cost: float | None
    cost_status: str
    created_at: datetime
    job_id: UUID | None = None
    rate_source: str | None = None
    prompt_rate_per_million: float | None = None
    completion_rate_per_million: float | None = None


@dataclass(frozen=True, slots=True)
class UsageCostBreakdown:
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


@dataclass(frozen=True, slots=True)
class UsageCostReport:
    calls: int
    tokens_prompt: int
    tokens_completion: int
    total_tokens: int
    usage_complete: bool
    cost_complete: bool
    known_estimated_cost: float
    estimated_cost: float | None
    breakdown: tuple[UsageCostBreakdown, ...] = field(default_factory=tuple)
    currency: str = "USD"
    cost_status: str = "unavailable"
    is_estimate: bool = True
    notice: str = (
        "Token counts are provider-reported when available. Costs are estimates from "
        "configured rates, not invoices. Unknown usage or rates are not treated as zero. "
        "Known totals cover recorded calls only; local compute and unreported charges "
        "are not measured."
    )
