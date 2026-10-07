"""Extend the existing traces, spans and cost ledger for Ticket #23."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d23b7e9a0142"
down_revision: str | Sequence[str] | None = "c22a4b8f901d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("traces", sa.Column("run_id", sa.Uuid(), nullable=True))
    for name, columns in (
        ("run_time", ["run_id", "start_time"]),
        ("correlation_time", ["correlation_id", "start_time"]),
        ("job_time", ["job_id", "start_time"]),
        ("user_time", ["user_id", "start_time"]),
        ("start_time", ["start_time"]),
    ):
        op.create_index("ix_traces_" + name, "traces", columns)
    for name in ("start_time", "end_time"):
        op.add_column("spans", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_spans_trace_time", "spans", ["trace_id", "start_time"])
    for name, target in (("trace_id", "traces.id"), ("span_id", "spans.id")):
        op.add_column(
            "cost_ledger",
            sa.Column(name, sa.Uuid(), sa.ForeignKey(target, ondelete="CASCADE"), nullable=True),
        )
    for name, length in (("provider", 100), ("model", 255), ("rate_source", 255)):
        op.add_column("cost_ledger", sa.Column(name, sa.String(length), nullable=True))
    for name in ("usage_status", "cost_status"):
        op.add_column(
            "cost_ledger",
            sa.Column(name, sa.String(32), nullable=False, server_default="unavailable"),
        )
    op.add_column("cost_ledger", sa.Column("total_tokens", sa.Integer(), nullable=True))
    for name in ("prompt_rate_per_million", "completion_rate_per_million"):
        op.add_column("cost_ledger", sa.Column(name, sa.Float(), nullable=True))
    for name in ("tokens_prompt", "tokens_completion", "cost"):
        op.alter_column("cost_ledger", name, nullable=True)
    op.create_unique_constraint("uq_cost_ledger_span", "cost_ledger", ["span_id"])
    op.create_index("ix_cost_ledger_trace_time", "cost_ledger", ["trace_id", "created_at"])
    op.create_check_constraint(
        "ck_cost_ledger_nonnegative",
        "cost_ledger",
        "(tokens_prompt IS NULL OR tokens_prompt >= 0) AND "
        "(tokens_completion IS NULL OR tokens_completion >= 0) AND "
        "(total_tokens IS NULL OR total_tokens >= 0) AND (cost IS NULL OR cost >= 0)",
    )


def downgrade() -> None:
    # A legacy NOT NULL schema cannot honestly represent unknown usage/cost.
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM cost_ledger WHERE tokens_prompt IS NULL "
            "OR tokens_completion IS NULL OR cost IS NULL)"
        )
    ):
        raise RuntimeError("Cannot downgrade while unknown usage/cost records exist.")
    op.drop_constraint("ck_cost_ledger_nonnegative", "cost_ledger", type_="check")
    op.drop_index("ix_cost_ledger_trace_time", table_name="cost_ledger")
    op.drop_constraint("uq_cost_ledger_span", "cost_ledger", type_="unique")
    for name in ("tokens_prompt", "tokens_completion", "cost"):
        op.alter_column("cost_ledger", name, nullable=False)
    for name in (
        "trace_id",
        "span_id",
        "provider",
        "model",
        "rate_source",
        "usage_status",
        "cost_status",
        "total_tokens",
        "prompt_rate_per_million",
        "completion_rate_per_million",
    ):
        op.drop_column("cost_ledger", name)
    op.drop_index("ix_spans_trace_time", table_name="spans")
    for name in ("start_time", "end_time"):
        op.drop_column("spans", name)
    for name in ("run_time", "correlation_time", "job_time", "user_time", "start_time"):
        op.drop_index("ix_traces_" + name, table_name="traces")
    op.drop_column("traces", "run_id")
