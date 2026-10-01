"""Persist job input, result, and execution metadata independently of Celery.

Revision ID: c83d20a19f04
Revises: 7f2a1c4b9e03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c83d20a19f04"
down_revision: str | Sequence[str] | None = "7f2a1c4b9e03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Legacy rows remain intact and are excluded from dispatch until their
    # owning feature supplies a registered operation and input.
    op.add_column("jobs", sa.Column("operation_type", sa.String(100), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("input_payload", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column("jobs", sa.Column("result_payload", postgresql.JSONB(), nullable=True))
    op.add_column("jobs", sa.Column("correlation_id", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "jobs",
        sa.Column(
            "cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_check_constraint(
        "ck_jobs_lifecycle",
        "jobs",
        "state IN ('PENDING', 'QUEUED', 'STARTED', 'COMPLETED', 'FAILED', 'CANCELLED')",
    )
    op.create_index("ix_jobs_dispatch", "jobs", ["state", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_dispatch", table_name="jobs")
    op.drop_constraint("ck_jobs_lifecycle", "jobs", type_="check")
    for column in (
        "cancellation_requested",
        "completed_at",
        "started_at",
        "correlation_id",
        "result_payload",
        "input_payload",
        "operation_type",
    ):
        op.drop_column("jobs", column)
