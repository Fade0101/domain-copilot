"""Persist execution leases and reuse retry/idempotency fields for Ticket #22."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c22a4b8f901d"
down_revision: str | Sequence[str] | None = "f19b6a2d9041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("operation_version", sa.String(64), nullable=False, server_default="1")
    )
    op.add_column("jobs", sa.Column("lease_owner", sa.String(255), nullable=True))
    for name in ("lease_acquired_at", "lease_expires_at", "paused_at", "last_dispatched_at"):
        op.add_column("jobs", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_recovery", "jobs", ["state", "lease_expires_at", "next_retry_at"])
    op.create_check_constraint(
        "ck_jobs_attempts", "jobs", "attempt_number >= 0 AND max_attempts >= 1"
    )
    op.create_check_constraint(
        "ck_jobs_lease", "jobs",
        "(lease_owner IS NULL AND lease_acquired_at IS NULL AND lease_expires_at IS NULL) "
        "OR (lease_owner IS NOT NULL AND lease_acquired_at IS NOT NULL "
        "AND lease_expires_at IS NOT NULL)",
    )
    # Reuse #19's existing unique job binding before a review snapshot exists.
    op.drop_constraint("ck_workflow_review_binding", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_review_binding", "workflow_runs",
        "review_snapshot IS NULL OR approval_job_id IS NOT NULL",
    )
    op.execute("""
        CREATE FUNCTION guard_workflow_job_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.approval_job_id IS NOT NULL AND (
                NEW.approval_job_id IS DISTINCT FROM OLD.approval_job_id
                OR NEW.user_id IS DISTINCT FROM OLD.user_id) THEN
                RAISE EXCEPTION 'An execution job binding is immutable';
            END IF;
            IF NEW.approval_job_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM jobs WHERE id = NEW.approval_job_id AND user_id = NEW.user_id
            ) THEN RAISE EXCEPTION 'Workflow and execution job owners must match'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER workflow_job_binding_immutable
        BEFORE INSERT OR UPDATE ON workflow_runs
        FOR EACH ROW EXECUTE FUNCTION guard_workflow_job_binding()
    """)
    op.execute("""
        CREATE FUNCTION guard_manual_retry_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.event_type = 'job.manual_retry' THEN
                RAISE EXCEPTION 'Manual retry audit events are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER manual_retry_event_immutable BEFORE UPDATE OR DELETE ON job_events
        FOR EACH ROW EXECUTE FUNCTION guard_manual_retry_event()
    """)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("""
        SELECT EXISTS(SELECT 1 FROM job_events WHERE event_type = 'job.manual_retry')
            OR EXISTS(SELECT 1 FROM jobs WHERE lease_owner IS NOT NULL)
            OR EXISTS(SELECT 1 FROM workflow_runs
                      WHERE approval_job_id IS NOT NULL AND review_snapshot IS NULL)
    """)):
        raise RuntimeError("Cannot discard active recovery bindings, claims or retry audit")
    op.execute("DROP TRIGGER manual_retry_event_immutable ON job_events")
    op.execute("DROP FUNCTION guard_manual_retry_event()")
    op.execute("DROP TRIGGER workflow_job_binding_immutable ON workflow_runs")
    op.execute("DROP FUNCTION guard_workflow_job_binding()")
    op.drop_constraint("ck_workflow_review_binding", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_review_binding", "workflow_runs",
        "(review_snapshot IS NULL) = (approval_job_id IS NULL)",
    )
    op.drop_constraint("ck_jobs_lease", "jobs", type_="check")
    op.drop_constraint("ck_jobs_attempts", "jobs", type_="check")
    op.drop_index("ix_jobs_recovery", table_name="jobs")
    for name in (
        "last_dispatched_at", "paused_at", "lease_expires_at", "lease_acquired_at",
        "lease_owner", "operation_version",
    ):
        op.drop_column("jobs", name)
