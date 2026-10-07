"""Durable review snapshots and immutable human decisions on existing tables.

Revision ID: f19b6a2d9041
Revises: e18a9d70c342

No workflow/job states change. Existing #18 approval rows retain their behavior.
Downgrade refuses to discard reviews/decisions/audit once #19 has been used.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f19b6a2d9041"
down_revision: str | Sequence[str] | None = "e18a9d70c342"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("approval_job_id", sa.Uuid(), nullable=True))
    op.add_column("workflow_runs", sa.Column("review_snapshot", JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_workflow_approval_job", "workflow_runs", "jobs", ["approval_job_id"], ["id"]
    )
    op.create_unique_constraint("uq_workflow_approval_job", "workflow_runs", ["approval_job_id"])
    op.create_check_constraint(
        "ck_workflow_review_binding",
        "workflow_runs",
        "(review_snapshot IS NULL) = (approval_job_id IS NULL)",
    )
    for name, column_type in (
        ("action", sa.String(16)),
        ("reviewer_role", sa.String(16)),
        ("draft_id", sa.String(64)),
        ("approved_draft_id", sa.String(64)),
        ("diff", sa.Text()),
    ):
        op.add_column("approvals", sa.Column(name, column_type, nullable=True))
    op.create_index(
        "uq_approval_managed_workflow",
        "approvals",
        ["workflow_run_id"],
        unique=True,
        postgresql_where=sa.text("action IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_approval_managed_decision",
        "approvals",
        "(action IS NULL AND draft_id IS NULL AND reviewer_role IS NULL "
        "AND approved_draft_id IS NULL AND diff IS NULL) OR COALESCE("
        "draft_id ~ '^[0-9a-f]{64}$' AND reviewer_role IN ('reviewer', 'admin') AND ("
        "(action = 'REJECT' AND status = 'REJECTED' AND approved_note IS NULL "
        "AND approved_draft_id IS NULL AND diff IS NULL "
        "AND length(btrim(rejection_reason)) BETWEEN 3 AND 4000) OR "
        "(action IN ('APPROVE', 'EDIT_AND_APPROVE') AND status = 'APPROVED' "
        "AND length(btrim(approved_note)) > 0 AND length(approved_note) <= 64000 "
        "AND approved_draft_id ~ '^[0-9a-f]{64}$' AND diff IS NOT NULL "
        "AND rejection_reason IS NULL)), FALSE)",
    )
    op.execute("""
        CREATE FUNCTION guard_clinical_review_snapshot() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.review_snapshot IS NOT NULL THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'A clinical review snapshot cannot be deleted';
                END IF;
                IF NEW.review_snapshot IS DISTINCT FROM OLD.review_snapshot
                    OR NEW.approval_job_id IS DISTINCT FROM OLD.approval_job_id
                    OR NEW.id IS DISTINCT FROM OLD.id
                    OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
                    RAISE EXCEPTION 'A clinical review snapshot and binding are immutable';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER clinical_review_snapshot_immutable
        BEFORE UPDATE OR DELETE ON workflow_runs
        FOR EACH ROW EXECUTE FUNCTION guard_clinical_review_snapshot()
    """)
    op.execute("""
        CREATE FUNCTION guard_human_approval_decision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.action IS NOT NULL THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'A human approval decision cannot be deleted';
                END IF;
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'A human approval decision is immutable';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER human_approval_decision_immutable
        BEFORE UPDATE OR DELETE ON approvals
        FOR EACH ROW EXECUTE FUNCTION guard_human_approval_decision()
    """)
    op.execute("""
        CREATE FUNCTION guard_human_approval_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.event_type IN (
                'approval.review_requested', 'approval.decision_recorded',
                'approval.finalization_requested'
            ) THEN
                RAISE EXCEPTION 'Human approval audit and signal events are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER human_approval_event_immutable
        BEFORE UPDATE OR DELETE ON job_events
        FOR EACH ROW EXECUTE FUNCTION guard_human_approval_event()
    """)


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS(SELECT 1 FROM workflow_runs WHERE review_snapshot IS NOT NULL)
            OR EXISTS(SELECT 1 FROM approvals WHERE action IS NOT NULL)
            OR EXISTS(SELECT 1 FROM job_events WHERE event_type IN (
                'approval.review_requested', 'approval.decision_recorded',
                'approval.finalization_requested'))
    """)
    )
    if populated:
        raise RuntimeError("Cannot discard persisted clinical reviews, decisions or audit")
    op.execute("DROP TRIGGER human_approval_event_immutable ON job_events")
    op.execute("DROP FUNCTION guard_human_approval_event()")
    op.execute("DROP TRIGGER human_approval_decision_immutable ON approvals")
    op.execute("DROP FUNCTION guard_human_approval_decision()")
    op.execute("DROP TRIGGER clinical_review_snapshot_immutable ON workflow_runs")
    op.execute("DROP FUNCTION guard_clinical_review_snapshot()")
    op.drop_constraint("ck_approval_managed_decision", "approvals", type_="check")
    op.drop_index("uq_approval_managed_workflow", table_name="approvals")
    for name in ("diff", "approved_draft_id", "draft_id", "reviewer_role", "action"):
        op.drop_column("approvals", name)
    op.drop_constraint("ck_workflow_review_binding", "workflow_runs", type_="check")
    op.drop_constraint("uq_workflow_approval_job", "workflow_runs", type_="unique")
    op.drop_constraint("fk_workflow_approval_job", "workflow_runs", type_="foreignkey")
    op.drop_column("workflow_runs", "review_snapshot")
    op.drop_column("workflow_runs", "approval_job_id")
