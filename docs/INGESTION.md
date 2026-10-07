# Document Ingestion — Ticket 8

Ticket #8 accepts PDF and Markdown, then runs **extract → clean → chunk → embed →
index** as `document.ingest` on Ticket #20's Celery worker. PostgreSQL owns the
original bytes, document progress, stage artifacts, chunks, embeddings and job
checkpoints. Redis carries job UUIDs and can be rebuilt from durable state.

## Start from a fresh checkout

Copy `.env.example` to `.env`. Set `POSTGRES_PASSWORD`, `DATABASE__URL`,
`AUTH__SECRET_KEY` (at least 32 characters) and a development-only
`AUTH__DEMO_PASSWORD` (at least 12 characters). Use host `postgres` in the
Compose database URL; the username, password and database must match the
`POSTGRES_*` values. See [JOBS.md](./JOBS.md#run-with-docker-compose) for the URL
shape and native Python setup.

```bash
docker compose up --build -d
docker compose ps
docker compose run --rm --build seed
```

The default stack runs PostgreSQL/pgvector, Redis, migrations, the API and a
separate Celery worker. No `jobs` profile is required. The optional seed command
uploads two synthetic sources through the authenticated HTTP API and waits for
completion. They contain no patient information or clinical recommendations and
are smoke examples, separate from Ticket #11's corpus.

Open `http://localhost:8000/docs`; liveness is at `/health`. The first ingestion
loads `all-MiniLM-L6-v2` from Hugging Face and needs network access to download
its public weights. Inference runs locally on CPU and does not call a chat LLM.
The worker's `embedding_cache` volume retains the weights across restarts. The
image runs as an unprivileged user with a writable model cache.

Run the seed command again: both responses should have `reused: true`, retain
the original document/job IDs and finish without adding chunks. With the default
configuration, the examples produce three Markdown chunks and one PDF chunk.
If port 5432, 6379 or 8000 is already occupied, stop the conflicting local service
or override that host port in a local Compose override file.

## Upload a source

Ingestion requires the admin `INGEST_DOCUMENTS` permission. Authenticate using
`POST /api/v1/auth/token`, or the API documentation's Authorize control. The
server derives ownership from the authenticated user.

`POST /api/v1/documents/ingest?filename=guide.md&version=1` accepts the **raw file
bytes** as its body. It does not accept multipart or JSON. Supply a plain
filename ending in `.pdf`, `.md` or `.markdown`; the filename is metadata and is
never opened as a server path. Use `application/pdf` or `text/markdown`.
`text/plain` is also accepted for Markdown, and `application/octet-stream` can
be used with either supported extension.

For example, with a bearer token in the Bash variable `TOKEN`:

```bash
curl -i 'http://localhost:8000/api/v1/documents/ingest?filename=guide.md&version=1' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: text/markdown' \
  --data-binary @guide.md
```

Successful acceptance returns **HTTP 202**, a `Location` header pointing to the
job and this response shape:

```json
{
  "document_id": "<document UUID>",
  "job_id": "<job UUID>",
  "state": "QUEUED",
  "status_url": "/api/v1/jobs/<job UUID>",
  "document_url": "/api/v1/documents/<document UUID>",
  "reused": false
}
```

The response state is the current job state; a duplicate of a completed upload
returns `COMPLETED` with HTTP 202 and `reused: true`. Acceptance waits only for
the bounded upload, database commit and best-effort enqueue. It does not wait
for extraction or model loading.

For a portable command-line client, set `INGEST_ACCESS_TOKEN` in the process
environment (or `AUTH__DEMO_PASSWORD` to log in as the development admin), then:

```bash
python scripts/ingest_documents.py guide.pdf guide.md --wait
python scripts/ingest_documents.py --seed --wait
```

The CLI also accepts `--api-url`, `--version` and `--timeout` (900 seconds by
default). It exits nonzero if an ingestion fails or polling times out; a timeout
does not cancel the job. On the host it reads credentials from the environment,
not automatically from `.env`. The Compose seed service passes the configured
demo password. With demo accounts disabled, pass an existing admin token using
`docker compose run --rm -e INGEST_ACCESS_TOKEN seed`.

The older metadata-only `POST /api/v1/documents` remains the in-memory
architecture demonstration returning 200/201. It does not upload, enqueue or
index a source; use `/documents/ingest` for this pipeline.

## Poll progress and failures

Use the same bearer token for both returned URLs:

| Endpoint | Returned information |
| --- | --- |
| `GET /api/v1/jobs/{id}` | Exact T7 state, result, safe error code and timestamps |
| `GET /api/v1/documents/{id}` | Filename, source hash, media type, version, document status, all five stages, latest job ID, chunk count and ingestion timestamp |

Document and stage statuses are `pending`, `processing`, `completed` or `failed`.
Stages include timestamps as work starts/finishes, an item count when an artifact
commits and an `error` on failure. Extract/clean counts represent text blocks;
chunk/embed/index counts represent chunks. A failed document also exposes
`error_stage` and `error_message`; unexpected exceptions become a fixed stage
message, while known input failures explain the required correction. Raw source
bytes, artifacts, vectors and internal job checkpoints are not returned by these
polling APIs. Reads enforce persisted ownership and the existing role grants.

Missing credentials return 401, denied access 403, unknown resources 404,
oversized uploads 413 and invalid filename/type/version/PDF signature 422.
Malformed PDFs, encrypted PDFs, non-UTF-8 Markdown and documents without
extractable text are accepted only far enough to produce a queryable failure at
`extract`. Storage unavailability returns a fixed 503. Once the document/source/job
transaction commits, an unavailable Redis broker does not undo the accepted job.

Polling remains available. Ticket #21 also exposes authenticated
`GET /api/v1/jobs/{id}/stream` for durable progress/replay and
`POST /api/v1/jobs/{id}/cancel` for persisted cooperative cancellation.
Disconnecting SSE does not cancel ingestion; see [JOB_STREAMING.md](./JOB_STREAMING.md).

## Chunking and citations

PDF extraction preserves one-based page numbers and uses bookmarks plus
conservative heading recognition. Scanned PDFs require OCR, which is not
implemented; encrypted or empty PDFs fail explicitly. Complex PDF reading order
and inferred headings should be checked against the original source. Markdown
uses CommonMark headings, including ATX and Setext syntax; its page is `null`.
Links and images are not fetched and embedded markup is never executed.

Cleaning normalizes Unicode and whitespace without inferring or rewriting
clinical claims. Adjacent blocks under the same heading hierarchy and page are
grouped, then split into **512-token windows with 64-token overlap** by default.
Overlap stays within that page/section. The configured embedding model's actual
tokenizer supplies offsets, so slices retain the source spelling and each
emitted fragment is checked against its token limit.

Chunk IDs are deterministic UUIDs derived from the immutable document ID,
chunker version, window settings, position, page, heading hierarchy and text
hash. Search hits from Ticket #9 retain the document ID/name/version, chunk ID,
section hierarchy, page, text snippet and ingestion timestamp. Dense hits also
include the stored embedding model, dimensions and version; keyword hits have
no embedding provenance.

The default MiniLM model has a shorter context than a 512-token chunk. Ingestion
embeds every model-sized subwindow, combines vectors by token weight and
normalizes the result for cosine search. This avoids silently discarding the
chunk's tail. [ADR-001](./adr/ADR-001-chunking-and-ingestion-embeddings.md) records
the tradeoffs; retrieval quality measurement belongs to #12. Existing pgvector
HNSW cosine and PostgreSQL FTS/GIN indexes are reused. Hybrid fusion and
reranking remain #10. Index batches become searchable as they commit; wait for
job completion when a caller requires a fully indexed document.

## Identity, retry and restart

Identity is scoped to the uploader and the tuple `(SHA-256(source bytes), media
type, version)`. The server computes the hash. `version` is a positive 32-bit
integer, default 1. Changed bytes or a different version create a new immutable
snapshot; changing only the filename reuses the first snapshot and its filename.
Concurrent duplicate uploads are serialized by a PostgreSQL constraint and row
lock, and share an active or completed job.

Chunking settings and embedding model/dimension/version are pinned on acceptance.
A worker with incompatible embedding configuration reports an explicit failure;
restore the original configuration before retrying. This is not an in-place
re-embedding API. Keep tokenizer/model artifacts consistent when resuming work.

| Durable boundary | Resume behavior |
| --- | --- |
| Source acceptance | Document, original bytes and PENDING job commit in one PostgreSQL transaction before Redis dispatch |
| Extract, clean, chunk | Reuse the saved stage artifact |
| Embedding batch | Reuse its saved vectors; completed batches are not recomputed |
| Index batch | Reuse its marker, or repeat an upsert with the same chunk/provenance keys if the process died between the index commit and marker |
| Job checkpoint | Store only artifact references; large texts/vectors stay in `ingestion_artifacts` |

For a lost Redis queue, restore Redis and run:

```bash
docker compose exec worker python -m app.core.jobs_cli reconcile --limit 100
```

For a confirmed interrupted `STARTED` job, restart the worker and explicitly
resume its existing UUID:

```bash
docker compose restart worker
docker compose exec worker python -m app.core.jobs_cli resume <job-UUID>
```

Reconciliation selects PENDING/QUEUED; it does not automatically resume STARTED
jobs. See [JOBS.md](./JOBS.md#recovery-from-redis-loss-or-worker-death) for the
operator contract. Worker execution locks prevent concurrent deliveries from
performing the same job simultaneously. Scheduling, heartbeat-based recovery,
retry/backoff policy and general request idempotency remain Ticket #22.

For a terminal failed/cancelled ingestion, fix the cause and submit the same
source/version again. This creates a **new job** attached to the existing
document and reuses completed artifacts. The old job stays terminal. This
ingestion-specific retry and deduplication does not change the generic T7
lifecycle or make generic `/jobs` submissions idempotent.

## Limits, storage and verification

| Setting | Default |
| --- | --- |
| `INGESTION__MAX_UPLOAD_BYTES` | 10,485,760 (10 MiB), enforced while reading the request |
| `INGESTION__MAX_PAGES` | 500 PDF pages |
| `INGESTION__MAX_CHARACTERS` | 2,000,000 extracted characters |
| `INGESTION__MAX_CHUNKS` | 4,096 |
| `INGESTION__CHUNK_TOKENS` / `CHUNK_OVERLAP` | 512 / 64 |
| `EMBEDDING__BATCH_SIZE` | 32 |
| `INGESTION__STAGE_TIMEOUT_SECONDS` | 600 per stage action or batch |

Parser/model calls run off the event loop. The timeout bounds the awaited
operation but does not forcibly terminate an already-running native/threaded
call; resource isolation still depends on worker/container limits.

Revision `95c7e8a12d40` follows Ticket #9's `4c1e9a7d52b8`, adds source/artifact
storage and citation fields, and preserves existing documents/vectors. It uses
Ticket #6's shared ORM metadata. `alembic check` must report no new operations.
Downgrading removes ingestion storage and is not a data-preserving rollback for
accepted uploads. Back up PostgreSQL, including source/artifact tables; their
foreign keys cascade when the owning document is deleted. No separate retention
scheduler or deletion API is added here.

Use only synthetic or public non-PII sources. This pipeline does not detect or
redact patient data. Original bytes and intermediate text are stored locally in
PostgreSQL; model weights download from Hugging Face, while embedding inference
stays local.

```bash
pytest tests/unit/application/test_ingestion.py tests/unit/infrastructure/test_document_extractors.py
pytest tests/unit/core/test_ingestion_settings.py tests/integration/test_ingestion_pipeline.py
```

The real-service integration suite requires `TEST_DATABASE_URL` (a disposable
PostgreSQL/pgvector admin URL) and `T20_TEST_REDIS_URL`. The broader T20 suite
also uses `T20_TEST_DATABASE_URL`. Missing services fail in CI rather than being
silently skipped. Tests cover format extraction, structure/token limits,
determinism, all stage failures, HTTP authorization/limits, concurrent duplicate
acceptance, Redis loss, a killed Celery worker, checkpoint replay, searchable
citation metadata and migration upgrade/downgrade preservation. These tests use
deterministic offline embedding/tokenizer doubles; the Docker seed smoke above
exercises the real model separately.
