"""Durable ingestion sources, stage artifacts and citation version metadata.

Revision ID: 95c7e8a12d40
Revises: 4c1e9a7d52b8

Additive for existing documents, chunks, embeddings and jobs. Source identity is
nullable for historical rows. Downgrade removes only Ticket 8's storage, so it
must not be used as a data-preserving rollback after accepting source uploads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "95c7e8a12d40"
down_revision: str | Sequence[str] | None = "4c1e9a7d52b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("media_type", sa.String(64), nullable=True))
    op.add_column(
        "documents", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )
    op.add_column("documents", sa.Column("ingestion_job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "documents_ingestion_job_id_fkey",
        "documents",
        "jobs",
        ["ingestion_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for name in ("ingestion_config", "ingestion_stages"):
        op.add_column(
            "documents",
            sa.Column(
                name, postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
            ),
        )
    op.add_column("documents", sa.Column("error_stage", sa.String(16), nullable=True))
    op.add_column("documents", sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "documents",
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint(
        "uq_document_ingestion_source",
        "documents",
        ["user_id", "content_hash", "media_type", "version"],
    )
    op.create_table(
        "document_sources",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_table(
        "ingestion_artifacts",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_key", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id", "artifact_key"),
    )
    op.add_column(
        "chunks",
        sa.Column("document_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("chunks", sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "ingested_at")
    op.drop_column("chunks", "document_version")
    op.drop_table("ingestion_artifacts")
    op.drop_table("document_sources")
    op.drop_constraint("uq_document_ingestion_source", "documents", type_="unique")
    op.drop_constraint("documents_ingestion_job_id_fkey", "documents", type_="foreignkey")
    for name in (
        "chunk_count",
        "ingested_at",
        "error_stage",
        "ingestion_stages",
        "ingestion_config",
        "ingestion_job_id",
        "version",
        "media_type",
        "content_hash",
    ):
        op.drop_column("documents", name)
