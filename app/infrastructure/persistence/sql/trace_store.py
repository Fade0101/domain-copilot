"""PostgreSQL traces and accounting on the original #6 tables."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import ObservabilityUnavailableError
from app.domain.observability.entities import (
    CostEntry,
    Span,
    SpanStatus,
    Trace,
    TraceQuery,
    UsageCostBreakdown,
)
from app.infrastructure.persistence.models import CostLedgerModel, SpanModel, TraceModel


def _trace(row: TraceModel) -> Trace:
    return Trace(
        id=row.id,
        user_id=row.user_id,
        job_id=row.job_id,
        run_id=row.run_id,
        correlation_id=row.correlation_id,
        start_time=row.start_time,
        end_time=row.end_time,
    )


def _conditions(query: TraceQuery, *, costs: bool = False) -> list[sa.ColumnElement[bool]]:
    conditions: list[sa.ColumnElement[bool]] = []
    for column, value in (
        (TraceModel.run_id, query.run_id),
        (TraceModel.job_id, query.job_id),
        (TraceModel.correlation_id, query.correlation_id),
        (TraceModel.user_id, query.user_id),
    ):
        if value is not None:
            conditions.append(column == value)
    time = CostLedgerModel.created_at if costs else TraceModel.start_time
    if query.start_time is not None:
        conditions.append(time >= query.start_time)
    if query.end_time is not None:
        conditions.append(time < query.end_time)
    return conditions


class PostgresTraceStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise ObservabilityUnavailableError("Trace storage is unavailable.") from exc

    async def record(
        self, trace: Trace, span: Span | None = None, cost: CostEntry | None = None
    ) -> None:
        async with self._transaction() as session:
            statement = insert(TraceModel).values(**asdict(trace))
            saved = await session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[TraceModel.id],
                    set_={
                        "start_time": sa.func.least(TraceModel.start_time, trace.start_time),
                        "end_time": sa.func.greatest(TraceModel.end_time, trace.end_time),
                        "run_id": sa.func.coalesce(TraceModel.run_id, trace.run_id),
                        "job_id": sa.func.coalesce(TraceModel.job_id, trace.job_id),
                        "correlation_id": sa.func.coalesce(
                            TraceModel.correlation_id, trace.correlation_id
                        ),
                    },
                    where=TraceModel.user_id == trace.user_id,
                ).returning(TraceModel.id)
            )
            if saved is None:
                raise ObservabilityUnavailableError("Trace identity does not match its owner.")
            if span is not None:
                await session.execute(
                    insert(SpanModel)
                    .values(**asdict(span))
                    .on_conflict_do_nothing(index_elements=[SpanModel.id])
                )
            if cost is not None:
                await session.execute(
                    insert(CostLedgerModel)
                    .values(**asdict(cost))
                    .on_conflict_do_nothing(index_elements=[CostLedgerModel.span_id])
                )

    async def get_trace(self, trace_id: UUID) -> Trace | None:
        async with self._transaction() as session:
            row = await session.get(TraceModel, trace_id)
            return _trace(row) if row else None

    async def query_traces(self, query: TraceQuery) -> list[Trace]:
        statement = (
            sa.select(TraceModel)
            .where(*_conditions(query))
            .order_by(TraceModel.start_time.desc(), TraceModel.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        async with self._transaction() as session:
            return [_trace(row) for row in (await session.scalars(statement)).all()]

    async def get_spans(self, trace_id: UUID, *, limit: int, offset: int) -> list[Span]:
        statement = (
            sa.select(SpanModel)
            .where(SpanModel.trace_id == trace_id)
            .order_by(SpanModel.start_time.asc().nullsfirst(), SpanModel.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._transaction() as session:
            return [
                Span(
                    id=row.id,
                    trace_id=row.trace_id,
                    name=row.name,
                    step_type=row.step_type,
                    inputs=row.inputs,
                    outputs=row.outputs,
                    status=SpanStatus(row.status),
                    tokens=row.tokens,
                    duration=row.duration,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
                for row in (await session.scalars(statement)).all()
            ]

    async def usage(self, query: TraceQuery) -> list[UsageCostBreakdown]:
        ledger = CostLedgerModel
        known_cost = sa.and_(ledger.cost_status == "estimated", ledger.cost.is_not(None))
        statement = (
            sa.select(
                ledger.provider,
                ledger.model,
                sa.func.count().label("calls"),
                sa.func.coalesce(sa.func.sum(ledger.tokens_prompt), 0).label("prompt"),
                sa.func.coalesce(sa.func.sum(ledger.tokens_completion), 0).label("completion"),
                sa.func.coalesce(sa.func.sum(ledger.total_tokens), 0).label("tokens"),
                sa.func.sum(
                    sa.case((ledger.usage_status != "provider_reported", 1), else_=0)
                ).label("unknown_usage"),
                sa.func.sum(sa.case((known_cost, 0), else_=1)).label("unknown_cost"),
                sa.func.sum(sa.case((known_cost, ledger.cost), else_=0)).label("known_cost"),
            )
            .join(TraceModel, TraceModel.id == ledger.trace_id)
            .where(*_conditions(query, costs=True))
            .group_by(ledger.provider, ledger.model)
            .order_by(ledger.provider, ledger.model)
        )
        async with self._transaction() as session:
            return [
                UsageCostBreakdown(
                    provider=row.provider or "unknown",
                    model=row.model or "unknown",
                    calls=row.calls,
                    tokens_prompt=row.prompt,
                    tokens_completion=row.completion,
                    total_tokens=row.tokens,
                    unavailable_usage_calls=row.unknown_usage,
                    unavailable_cost_calls=row.unknown_cost,
                    known_estimated_cost=float(row.known_cost),
                    estimated_cost=float(row.known_cost) if row.unknown_cost == 0 else None,
                )
                for row in (await session.execute(statement)).all()
            ]
