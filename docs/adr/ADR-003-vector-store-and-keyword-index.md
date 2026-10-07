# ADR-003: Vector store and keyword index (pgvector + PostgreSQL FTS)

**Status:** Accepted · **Ticket:** #9 · **Requirements:** BRD AC-2.1, AR-7; SDD A.5.5, A.8.2, B.1.2

## Context

Hybrid retrieval (BRD AC-2.1) needs two independently-ranked result sets: dense
vector similarity and sparse keyword relevance. Fusion, re-ranking and the
evidence threshold sit above them and are out of scope here. The decisions this
ticket had to make were therefore: *where do vectors live*, *which index serves
them*, *how is keyword search implemented*, and *how is a stored vector made
interpretable later*.

The initial schema (revision `addc7d39b90f`) had declared a single
`chunks.embedding VECTOR(1536)` column with no indexes, no citation columns and
no record of which model produced a vector. 1536 is an OpenAI embedding width,
whereas the configured provider is `all-MiniLM-L6-v2` at **384** dimensions, so
that column could never have held a valid vector. Nothing wrote to it — there was
no SQL adapter at all — so correcting it cost no data.

## Decision

**1. PostgreSQL + pgvector, not a separate vector service.** One database holds
relational rows and vectors, one Alembic chain migrates both, and compose stays a
two-service stack (SDD B.1.2). A dedicated vector database would add an
operational component and a second consistency boundary for a corpus of this
size. The `IRetrievalStore` port keeps that reversible: swapping in a dedicated
store is an adapter change with no domain or application edits.

**2. Embeddings live in their own `chunk_embeddings` table,** keyed uniquely by
`(chunk_id, embedding_model, embedding_version)` — the shape SDD A.8.2 specifies.
A chunk may therefore hold several vectors at once, which is what makes
re-embedding safe: a new model or version inserts alongside the live vectors
instead of overwriting them, and the switch-over is a configuration change. The
same key makes ingestion idempotent, since a repeated upsert updates in place.

**3. HNSW with `vector_cosine_ops`, not IVFFlat.** An IVFFlat index must be built
against populated data to train its centroid lists, so its quality depends on
*when* in the pipeline the migration ran, and an index built on an empty table is
degenerate. HNSW builds incrementally and is therefore reproducible from a clean
database — a requirement for migration-managed indexes and for CI, which
provisions an empty PostgreSQL on every run. The opclass matches the cosine
distance operator (`<=>`) the adapter orders by; a different opclass would leave
the index unused.

**4. Cosine distance, reported as similarity.** pgvector returns distance, so the
adapter converts with `score = 1 - distance`, yielding ordinary cosine similarity
in `[-1, 1]`. Dense search is additionally scoped to the configured model and
version, because distances between vectors from different embedding spaces are
not comparable — and decision 2 makes such rows genuinely present.

**5. Keyword search uses a stored generated `tsvector` + GIN.**
`chunks.content_tsv` is `GENERATED ALWAYS AS (to_tsvector('english', text))
STORED`, so PostgreSQL maintains it and it can never drift from `text`. The
two-argument `to_tsvector(regconfig, text)` form is required because it is
`IMMUTABLE`; the one-argument form depends on a session setting and PostgreSQL
rejects it in a generated column. Queries use `plainto_tsquery('english', …)`
with the query text bound as a parameter, and rank with `ts_rank`. No extra
service is introduced (SDD B.1.2).

**7. The store reuses the application's existing `Database` connection** rather
than opening a second pool, and therefore connects over the `asyncpg` driver
already established for persistence. pgvector's SQLAlchemy `Vector` type works
over asyncpg, so sharing one pool costs nothing and avoids a second driver in
the dependency surface.

**6. Provenance travels with results.** Every dense hit reports the
`embedding_model`, `embedding_dim` and `embedding_version` of the row it actually
matched, read back from storage rather than assumed from configuration. Keyword
hits report `None` for all three, because a full-text match involves no
embedding and inventing provenance would be fabricated citation metadata.

## Consequences

- Vector and keyword recall are bounded by what one PostgreSQL instance can
  serve. SDD A.5.5 already records the exit: beyond roughly 1M chunks, swap the
  adapter behind `IRetrievalStore`.
- HNSW costs more to build and more memory than IVFFlat at equal recall, and its
  `m`/`ef_construction` parameters are left at pgvector defaults. Tuning is
  deferred until there is a real corpus to measure against (ticket #11).
- `score` is comparable only *within* one ranking: cosine similarity and
  `ts_rank` are different scales. Normalising across them is exactly the problem
  RRF solves, one layer up.
- The English text-search configuration is hard-coded; a multilingual corpus
  would need a per-document configuration and a rebuilt generated column.
- Re-embedding grows `chunk_embeddings` monotonically. Reclaiming superseded
  generations needs a cleanup step that does not exist yet.
- `downgrade()` restores `chunks.embedding VECTOR(1536)` for schema fidelity but
  cannot restore vector data. Nothing had ever been written to it.

## Enforcement

- `sqlalchemy`, `alembic`, `psycopg`, `asyncpg` and `pgvector` are forbidden in
  `app.domain` and `app.application` by the import-linter contracts in
  `pyproject.toml` and by the AST scan in
  `tests/architecture/test_boundaries.py`. A unit test additionally asserts the
  retrieval port module imports stdlib only.
- `tests/integration/test_retrieval_store.py` runs against real PostgreSQL with
  the real pgvector extension, asserting cosine ordering, `ts_rank` scoring,
  index definitions (`USING hnsw … vector_cosine_ops`, `USING gin`), the stored
  generated column, upsert idempotency and the 384-dimension column width. It
  skips when no database is reachable locally but **fails** rather than skips in
  CI, so the coverage cannot quietly disappear.
- CI provisions `pgvector/pgvector:pg16` and runs `alembic upgrade head` twice,
  proving the schema builds from empty and that re-running is a no-op.

## Notes

The frozen SDD names this port `IVectorStore` with a single `search()` method
(§A.3.2 diagram) and the adapter `PostgresVectorAdapter` (§A.5.5 scaling note).
This ticket implements `IRetrievalStore` with separate `dense_search()` and
`keyword_search()` methods, and `PostgresRetrievalStore`. The split is required
by AC-2.1 — RRF needs two independent rankings, which a single `search()` cannot
express — and the name reflects that the store now owns the keyword index too,
not only vectors. The SDD is frozen and has not been edited; this note records
the divergence.
