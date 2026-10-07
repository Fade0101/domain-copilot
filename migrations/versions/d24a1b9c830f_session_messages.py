"""Add durable session messages for Ticket #24's history contract.

Revision ID: d24a1b9c830f
Revises: c22a4b8f901d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d24a1b9c830f"
down_revision = "c22a4b8f901d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(9), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_session_messages_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_session_messages_sequence"),
        sa.CheckConstraint(
            "(role = 'user' AND answer IS NULL) OR (role = 'assistant' AND answer IS NOT NULL)",
            name="ck_session_messages_role",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM session_messages)")):
        raise RuntimeError("Cannot discard persisted session history.")
    op.drop_table("session_messages")
