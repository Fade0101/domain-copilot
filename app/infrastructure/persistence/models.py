"""SQLAlchemy ORM models (Ticket #6).

Declarative mappings for every table the Alembic migrations create. Their one job
is to be an exact mirror of the migrated schema, so that:

* ``alembic revision --autogenerate`` can diff the models against a database and
  write a correct migration, instead of revisions being hand-authored; and
* repositories can be written against mapped classes rather than ``text()``.

**The migrations remain the source of truth.** These models were derived from the
schema through ``c83d20a19f04`` (Tickets #5, #6 and #20), introspected from a
migrated database. ``tests/integration/
test_orm_models.py`` asserts the two agree by running autogenerate against a
migrated database and requiring an empty diff, so a model edited out of step with
the schema fails rather than silently producing a wrong migration.

Two details worth knowing before editing:

* **Enums are non-native.** The migrations use ``sa.Enum(..., native_enum=False)``,
  which emits ``VARCHAR(n)`` sized to the longest member and *no* check constraint
  (SQLAlchemy 2.0 defaults ``create_constraint`` to ``False``). The declarations
  below must keep both flags and the same member order, or autogenerate reports a
  type change.
* **``metadata`` is a reserved name.** ``documents`` and ``chunks`` both have a
  column called ``metadata``, which collides with ``DeclarativeBase.metadata``.
  The attribute is named ``metadata_`` and mapped explicitly to the column.

This module is pure persistence mapping: it imports no domain types and contains
no business rules. Mapping between these rows and domain entities is a
repository's job.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Dimensionality of the stored embeddings, matching the migration's
#: ``Vector(1536)``. The configured embedding model must agree with this; changing
#: it requires a migration, not just a settings edit.
EMBEDDING_DIMENSIONS = 1536

_ROLE_ENUM = sa.Enum("analyst", "reviewer", "admin", name="role_enum", native_enum=False)
_DOCUMENT_STATUS_ENUM = sa.Enum(
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    name="document_status_enum",
    native_enum=False,
)
_JOB_STATE_ENUM = sa.Enum(
    "PENDING",
    "QUEUED",
    "STARTED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    name="job_state_enum",
    native_enum=False,
)
_WORKFLOW_STATE_ENUM = sa.Enum(
    "AWAITING_APPROVAL",
    "REJECTED",
    "APPROVED",
    name="workflow_state_enum",
    native_enum=False,
)
_APPROVAL_STATUS_ENUM = sa.Enum(
    "PENDING", "APPROVED", "REJECTED", name="approval_status_enum", native_enum=False
)
_SPAN_STATUS_ENUM = sa.Enum("OK", "ERROR", name="span_status_enum", native_enum=False)


def _now() -> sa.TextClause:
    """Server-side ``now()``, matching the migrations' ``server_default``."""
    return sa.text("now()")


class Base(DeclarativeBase):
    """Declarative base. ``Base.metadata`` is what ``migrations/env.py`` diffs."""


class UserModel(Base):
    """A registered user (BRD FR-8). The server-side identity of record."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True, unique=True)
    hashed_password: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[str] = mapped_column(_ROLE_ENUM, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class DocumentModel(Base):
    """An ingested source document (FR-1), owned by the user who registered it."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), sa.ForeignKey("users.id"), nullable=False)
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(_DOCUMENT_STATUS_ENUM, nullable=False)
    error_message: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # "metadata" is taken by DeclarativeBase; see the module docstring.
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class ChunkModel(Base):
    """A retrievable passage of a document plus its embedding (FR-2)."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        # Cascade: a chunk has no meaning without its document, and the migration
        # declares it, so the model must too or autogenerate reports a drift.
        sa.Uuid(),
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # Nullable: a chunk is written before it is embedded.
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB(), nullable=False)


class JobModel(Base):
    """An asynchronous job (T7), owned by the user who started it."""

    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('PENDING', 'QUEUED', 'STARTED', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_jobs_lifecycle",
        ),
        sa.Index("ix_jobs_dispatch", "state", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(_JOB_STATE_ENUM, nullable=False)
    # Uniqueness rejects colliding keys. Ticket #20 uses a fresh job UUID;
    # canonical-input request deduplication belongs to Ticket #22.
    idempotency_key: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, index=True, unique=True
    )
    attempt_number: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    next_retry_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    checkpoint_data: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    # Nullable for legacy rows, which the runner excludes from reconciliation.
    operation_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancellation_requested: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class JobEventModel(Base):
    """One ordered event in a job's lifecycle, for replayable progress streams."""

    __tablename__ = "job_events"
    # Per-job monotonic sequence: the uniqueness that lets a consumer detect a
    # gap or a duplicate rather than silently missing an event.
    __table_args__ = (
        sa.UniqueConstraint("job_id", "sequence_number", name="uq_job_event_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        # Cascade: an event log outliving its job would be unreachable rows.
        sa.Uuid(),
        sa.ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class WorkflowRunModel(Base):
    """A clinical workflow run (FR-5), owned by the analyst who started it."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    correlation_id: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, index=True, unique=True
    )
    case_summary: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    state: Mapped[str] = mapped_column(_WORKFLOW_STATE_ENUM, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class ApprovalModel(Base):
    """A reviewer's decision on a workflow run (FR-5; audit trail for #19)."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False
    )
    # The actor. Not nullable: an approval with no attributable reviewer would be
    # an audit record that proves nothing.
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("users.id"), nullable=False
    )
    original_note: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # Null until a decision edits or approves the note.
    approved_note: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[str] = mapped_column(_APPROVAL_STATUS_ENUM, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class TraceModel(Base):
    """An execution trace (FR-9), owned by the user whose request produced it."""

    __tablename__ = "traces"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    # Nullable: a synchronous request produces a trace with no job behind it.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    start_time: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    # Null while the trace is still open.
    end_time: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class SpanModel(Base):
    """One step within a trace: an agent, tool, or LLM call (FR-9)."""

    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    trace_id: Mapped[uuid.UUID] = mapped_column(
        # Cascade: spans are parts of a trace, not independent records.
        sa.Uuid(),
        sa.ForeignKey("traces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    step_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    tokens: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    status: Mapped[str] = mapped_column(_SPAN_STATUS_ENUM, nullable=False)
    duration: Mapped[float | None] = mapped_column(sa.Float(), nullable=True)


class CostLedgerModel(Base):
    """Token and cost accounting per job (FR-9). Holds no secret material."""

    __tablename__ = "cost_ledger"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    # Nullable: a synchronous call incurs cost without a job.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=True
    )
    tokens_prompt: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    tokens_completion: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    cost: Mapped[float] = mapped_column(sa.Float(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class SessionModel(Base):
    """A persistent conversation session (AC-7.4), owned by one user.

    Added by migration ``7f2a1c4b9e03`` for the Ticket #5 ownership checks.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
