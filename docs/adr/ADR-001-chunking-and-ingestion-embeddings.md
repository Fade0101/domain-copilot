# ADR-001: Structure-aware chunking and ingestion embeddings

**Status:** Accepted · **Date:** 2026-10-02 · **Ticket:** #8

**Requirements addressed:** BRD AC-1.1–1.7, AC-2.4, AR-6/7/8/9, T7-02/05;
SDD §A.4.2, §A.5.5, §A.5.7, §A.8.2.

## Context

Clinical reference documents separate concepts through headings such as
contraindications, interactions and dosage. A chunk crossing those boundaries
can blur the context of a claim. Citations also need the immutable document
revision, source page and exact passage identity.

The SDD specifies structure-aware 512-token windows with 64-token overlap.
Ticket #7's default `all-MiniLM-L6-v2` model accepts a shorter input. Sending a
whole 512-token chunk to that model would silently truncate its tail. Ticket
#9 supplies independent dense and keyword indexes, and Ticket #20 supplies
at-least-once job execution with named PostgreSQL checkpoints.

## Decision

1. **Parse PDF and Markdown through an extraction port.** Use `pypdf` for text
   PDFs and `markdown-it-py` for CommonMark. PDF blocks carry one-based pages,
   bookmarks and conservative inferred headings; Markdown blocks carry the
   heading hierarchy and no invented page number. Links are not fetched and
   source content is never executed. OCR and encrypted PDFs are unsupported
   and produce explicit failures.

2. **Preserve structure before applying token windows.** Normalize Unicode and
   whitespace, group adjacent blocks sharing a page and heading hierarchy, then
   apply the configured 512/64 defaults. Overlap never crosses page/section
   boundaries. Smaller final chunks are retained, including short headings.
   Token offsets come from the embedding model's tokenizer through a plain-data
   `ITextTokenizer` port. Recheck each emitted fragment's token count, since
   slicing at a subword boundary can change tokenization.

3. **Use deterministic source and passage identities.** The source UUID derives
   from uploader, server-computed SHA-256, media type and caller-supplied version.
   The chunk UUID derives from that source UUID, chunker version, window sizes,
   position, page, heading hierarchy and text hash. Renaming an identical upload
   retains its original name and identity. Changed content or a new version
   creates a distinct snapshot, preserving earlier citations.

4. **Embed every token in a stored chunk.** Split it into nonoverlapping
   subwindows within the model's effective context, excluding special-token
   overhead. Weight each embedding by its token count, combine and normalize
   for cosine search. Short chunks keep their original vector direction. This
   preserves the SDD's chunk size without silently dropping text or changing
   the configured model. Persist model, dimensions and version with each vector.
   A different embedding recipe/model needs a new provenance version and
   evaluation; it must not silently overwrite the current generation.

5. **Commit source acceptance atomically with the job.** The ingestion store
   writes the document, original bytes and a prepared PENDING job in one
   PostgreSQL transaction. The generic job service then transitions/dispatches
   the committed job. A unique source constraint and row lock make simultaneous
   duplicates share an active/completed job. A failed/cancelled source can
   receive a new job while retaining committed artifacts and the old terminal
   job. This is a domain rule; general retry/idempotency policy remains #22.

6. **Persist artifacts at stage and batch boundaries.** Save extract/clean/chunk
   outputs and individual embedding batches in PostgreSQL. Save only artifact
   references in T7 checkpoints. Reuse an artifact committed before a worker
   crash even if its job checkpoint was not yet written. Index batches use the
   existing deterministic chunk/provenance upsert keys, so replay after an index
   commit cannot multiply rows. Document progress and safe errors are queryable
   separately from the exact six-state job lifecycle.

7. **Reuse the existing schema and runner.** Extend Ticket #6's ORM and the
   linear Alembic chain with revision `95c7e8a12d40`; retain Ticket #9's
   `chunks`, `chunk_embeddings`, HNSW cosine and FTS/GIN indexes. Register the
   real handler in the T20 composition root with lazy model loading. Worker
   async sessions use `NullPool` because Celery creates a fresh event loop per
   task; API sessions share the existing database pool.

8. **Expose raw uploads and polling.** `/api/v1/documents/ingest` validates a
   bounded byte stream, authorizes the admin capability and returns HTTP 202
   with job/document URLs. This separate path preserves the existing
   metadata-only registration slice. Streaming/cancel transport remains #21.

## Consequences

- Page/heading boundaries improve citation precision but can produce small
  chunks and split context across pages. PDF heading/reading-order heuristics
  are imperfect; citations retain the page for checking against the source.
- Token-weighted pooling covers the full chunk but may dilute a small relevant
  span. Ticket #12 must measure retrieval quality. A longer-context model can
  replace this provider after an explicit provenance/schema decision.
- Keeping original bytes and artifacts makes restarts independent of local
  files and Redis, at the cost of PostgreSQL storage. Backups must include
  those tables; deletion cascades from the owning document.
- Chunking and embedding settings are captured on upload. A worker with a
  different model/dimension/version fails explicitly instead of mixing vectors.
  Operators must restore the compatible configuration to resume that snapshot.
- Committed index batches are individually visible to search. There is no
  corpus-wide publication switch in this ticket.
- ADR-003 remains the store/index decision. **Hybrid fusion, reranking,
  thresholds and query orchestration remain Ticket #10**; this ADR records the
  ingestion portion of the reserved chunking/retrieval topic and can be extended
  when that decision is implemented. The frozen SDD is unchanged.

## Enforcement

- `tests/unit/application/test_ingestion.py` verifies structure/token bounds,
  deterministic IDs, complete embedding coverage, stage failures and checkpoint
  replay through framework-free ports.
- `tests/unit/infrastructure/test_document_extractors.py` exercises real PDF and
  Markdown parsers with synthetic inputs.
- `tests/integration/test_ingestion_pipeline.py` runs the production handler on
  real PostgreSQL/pgvector, Redis and a separate Celery worker, using offline
  embedding doubles. It checks concurrent duplicates, worker interruption,
  Redis loss, HTTP boundaries, search metadata and migration preservation.
- The [Docker seed workflow](../INGESTION.md) exercises both formats with the
  actual local model and can be repeated to verify idempotency.
- Architecture tests and import-linter keep parser/model/database SDKs outside
  domain/application code. CI upgrades migrations twice and runs `alembic check`.
