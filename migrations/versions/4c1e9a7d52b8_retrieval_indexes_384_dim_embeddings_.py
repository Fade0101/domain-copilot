"""Retrieval indexes: 384-dim embeddings, citation metadata and FTS

Revision ID: 4c1e9a7d52b8
Revises: c83d20a19f04
Create Date: 2026-09-30 15:20:00.000000

Reshapes the chunk schema for hybrid retrieval (BRD AC-2.1, SDD A.5.5/A.8.2):

* ``chunks.embedding VECTOR(1536)`` is removed. It was sized for an OpenAI
  model, but the configured provider is ``all-MiniLM-L6-v2``, which emits 384
  dimensions -- the column could never have held a valid vector. Nothing wrote
  to it (there was no SQL adapter), so no data is lost in practice.
* Embeddings move to their own ``chunk_embeddings`` table, as SDD A.8.2
  specifies, keyed by chunk *and* provenance so the corpus can be re-embedded
  under a new model/version without destroying the live index.
* ``chunks`` gains the ``section``/``page``/``token_count`` citation columns
  from SDD A.8.2 that BRD AC-2.5 requires on every answer.
* A stored generated ``content_tsv`` plus a GIN index provide the sparse half
  of hybrid retrieval; an HNSW index provides the dense half.

``downgrade()`` restores the previous shape, including the 1536-dim column. It
cannot restore vector *data*, which is called out here rather than pretended
away.
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4c1e9a7d52b8"
# Rebased from 7f2a1c4b9e03 onto c83d20a19f04 (Ticket #20's durable job fields)
# when this work was integrated: both revisions descended from 7f2a1c4b9e03, and
# leaving this pointed there would have left the chain with two heads.
down_revision: str | Sequence[str] | None = "c83d20a19f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dimensionality of the configured embedding model. Deliberately a literal
# rather than an import from ``app.core.config``: a migration is a historical
# snapshot and must keep applying identically even after application defaults
# change. ``app/infrastructure/persistence/sql/tables.py`` holds the runtime
# constant, and the integration suite asserts the two agree.
_EMBEDDING_DIM = 384

# Superseded dimensionality, restored on downgrade for fidelity only.
_LEGACY_EMBEDDING_DIM = 1536

_HNSW_INDEX = "ix_chunk_embeddings_embedding_hnsw"
_TSV_INDEX = "ix_chunks_content_tsv"
_DOCUMENT_ID_INDEX = "ix_chunks_document_id"
_PROVENANCE_CONSTRAINT = "uq_chunk_embedding_provenance"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("chunks", "embedding")

    op.add_column("chunks", sa.Column("section", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("page", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("token_count", sa.Integer(), nullable=True))

    # STORED (not VIRTUAL) so the GIN index below has something to index.
    # The two-argument ``to_tsvector(regconfig, text)`` is IMMUTABLE, which a
    # generated column requires; the one-argument form is only STABLE because it
    # depends on the session's ``default_text_search_config``, and PostgreSQL
    # rejects it here.
    op.add_column(
        "chunks",
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
    )

    op.create_index(
        _TSV_INDEX,
        "chunks",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    # Every citation join filters chunks by document; the foreign key alone does
    # not create an index in PostgreSQL.
    op.create_index(_DOCUMENT_ID_INDEX, "chunks", ["document_id"], unique=False)

    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=_EMBEDDING_DIM),
            nullable=False,
        ),
        sa.Column("embedding_model", sa.String(length=255), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("embedding_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Makes re-indexing idempotent: the adapter's ON CONFLICT targets this
        # constraint by name. Including model and version (rather than chunk_id
        # alone) is what allows a re-embedded corpus to coexist with the old one.
        sa.UniqueConstraint(
            "chunk_id",
            "embedding_model",
            "embedding_version",
            name=_PROVENANCE_CONSTRAINT,
        ),
    )

    # HNSW rather than IVFFlat: an IVFFlat index must be built against populated
    # data to train its centroid lists, which makes index quality depend on
    # *when* the migration ran. HNSW builds incrementally and is therefore
    # reproducible from a clean database -- a requirement of this ticket.
    # vector_cosine_ops matches the cosine distance operator the adapter uses;
    # an index with a different opclass would simply not be used.
    op.create_index(
        _HNSW_INDEX,
        "chunk_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_HNSW_INDEX, table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")

    op.drop_index(_DOCUMENT_ID_INDEX, table_name="chunks")
    op.drop_index(_TSV_INDEX, table_name="chunks")

    op.drop_column("chunks", "content_tsv")
    op.drop_column("chunks", "token_count")
    op.drop_column("chunks", "page")
    op.drop_column("chunks", "section")

    # Restores the column as it was declared in addc7d39b90f (nullable, 1536).
    # Vector data cannot be restored; it was never written.
    op.add_column(
        "chunks",
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=_LEGACY_EMBEDDING_DIM),
            nullable=True,
        ),
    )
