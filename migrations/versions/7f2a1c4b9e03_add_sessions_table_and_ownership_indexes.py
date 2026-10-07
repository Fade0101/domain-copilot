"""Add sessions table and ownership indexes

Revision ID: 7f2a1c4b9e03
Revises: addc7d39b90f
Create Date: 2026-09-30 10:12:44.137902

Ticket #5 (Authentication & RBAC) needs two things the initial schema does not
provide, and nothing else. It does not alter or re-create anything from
``addc7d39b90f``.

1. ``sessions``. Object-ownership checks are required for runs, jobs, traces and
   sessions. The first three have ``user_id`` columns already; ``sessions`` was
   not created, though it is specified in the frozen SDD (the data-model table at
   docs/SYSTEM-DESIGN.md:816 -- ``id, user_id, title, created_at`` -- and the
   ``users >--< sessions`` relationship at :780). The columns here are exactly
   those four, so this fills the documented gap rather than designing a table.

2. Indexes on the ``user_id`` foreign keys of ``jobs``, ``workflow_runs`` and
   ``traces``. The per-object check reads one row by primary key and needs no
   index, but the AC-8.2 grants it backs -- "view own runs/jobs/traces" -- are
   filters on ``user_id``, which would otherwise be sequential scans. PostgreSQL
   does not index a foreign key automatically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f2a1c4b9e03"
down_revision: str | Sequence[str] | None = "addc7d39b90f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        # NOT NULL: a session with no owner would be a row no ownership check
        # could ever authorize, since an absent owner is never treated as a match.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_jobs_user_id"), "jobs", ["user_id"], unique=False)
    op.create_index(op.f("ix_workflow_runs_user_id"), "workflow_runs", ["user_id"], unique=False)
    op.create_index(op.f("ix_traces_user_id"), "traces", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_traces_user_id"), table_name="traces")
    op.drop_index(op.f("ix_workflow_runs_user_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_jobs_user_id"), table_name="jobs")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
