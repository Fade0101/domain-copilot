"""Distinguish finalized notes from existing approvals and their reviewed drafts.

Revision ID: e18a9d70c342
Revises: 95c7e8a12d40

No approval columns or workflow states change. Downgrading removes final notes;
it is not a data-preserving rollback after finalization has been used.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e18a9d70c342"
down_revision: str | Sequence[str] | None = "95c7e8a12d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "final_clinical_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("finalized_by", sa.Uuid(), nullable=False),
        sa.Column(
            "finalized_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.ForeignKeyConstraint(["finalized_by"], ["users.id"]),
        sa.UniqueConstraint("workflow_run_id", name="uq_final_note_workflow"),
        sa.UniqueConstraint("approval_id", name="uq_final_note_approval"),
        sa.CheckConstraint("draft_id ~ '^[0-9a-f]{64}$'", name="ck_final_note_draft_digest"),
        sa.CheckConstraint(
            "length(btrim(note)) > 0 AND length(note) <= 64000", name="ck_final_note_content"
        ),
    )


def downgrade() -> None:
    op.drop_table("final_clinical_notes")
