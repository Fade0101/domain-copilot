# Async Jobs — Ticket 20

Celery workers consume UUIDs from Redis. PostgreSQL stores job ownership, input,
lifecycle, checkpoints, result and error. The implementation is shared by future
ingestion (#8), retrieval evaluation (#12) and workflow (#17) handlers.

## Run with Docker Compose

Copy `.env.example` to `.env` and configure these local values:

| Setting | Value to provide |
| --- | --- |
| `POSTGRES_PASSWORD` | A local database password; no default password is supplied |
| `DATABASE__URL` | PostgreSQL URL for that user/password/database, with host `postgres` for Compose |
| `AUTH__SECRET_KEY` | A random signing key of at least 32 characters |
| `AUTH__DEMO_PASSWORD` | A development-only password of at least 12 characters for demo login |

For example, the URL shape is
`postgresql://postgres:<url-encoded-password>@postgres:5432/domain_copilot`.
Replace the placeholder locally. Never commit credentials. Generate a random
value with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

```bash
docker compose --profile jobs up --build -d
docker compose --profile jobs ps
docker compose --profile jobs logs worker
```

The profile starts PostgreSQL and Redis, runs `alembic upgrade head`, then starts
the API and a separate Celery worker. The image runs as an unprivileged user and
installs the CPU version of PyTorch for local embeddings. The API is available at
`http://localhost:8000/docs`; database and broker ports bind to loopback only.
The API and worker use the same database and queue settings. In development,
the configured demo password seeds `admin@example.com`, `reviewer@example.com`
and `analyst@example.com` through Ticket #5's existing startup flow.

For native Python development, use host `localhost` in `DATABASE__URL`, start
the two services with `docker compose up -d postgres redis`, and run these in
separate terminals after installing `requirements.txt`:

```bash
alembic upgrade head
uvicorn app.presentation.api.app:create_app --factory --reload
celery -A app.core.worker:celery_app worker --loglevel=INFO --concurrency=2
```

Use Docker for workers on Windows. The portable integration tests use Celery's
`solo` pool in a separate process; the supplied Linux image uses prefork workers.
The worker requires PostgreSQL and never falls back to an in-memory job store.

## Submit and poll

Authenticate through `POST /api/v1/auth/token` with the configured admin account,
or use the API documentation's Authorize control with the resulting bearer token.
With that token in `TOKEN`:

```bash
curl -i http://localhost:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operation_type":"diagnostic","payload":{}}'
```

The response is **HTTP 202 Accepted** with a `Location` header and:

```json
{
  "job_id": "<UUID>",
  "state": "QUEUED",
  "status_url": "/api/v1/jobs/<UUID>"
}
```

`GET /api/v1/jobs/<UUID>` returns `state`, `result`, `error`, timestamps,
`correlation_id`, and ownership metadata. Before completion, `result` is null.
A completed diagnostic job returns `{"ok": true}`. A failed handler stores
`JOB_HANDLER_FAILED`, without its exception text. Inputs and checkpoints are
not part of the public response. Ticket #5's `resource_id`, `resource_type` and
`owner_id` fields remain available alongside `job_id`.

The generic submission endpoint requires `MANAGE_ALL_JOBS` (admin). A caller
cannot set its owner, role or executable code in the request. Polling requires
ownership or the documented admin grant. Analyst/reviewer feature routes in
#8/#12/#17 will apply their own permissions before calling `JobService.submit`.
Registering a handler does not bypass domain authorization or approval gates.

Unknown operations and invalid/oversized JSON return 422; missing credentials
return 401; denied access returns 403; missing jobs return 404. A job-store failure
returns a fixed 503 response. Redis publishing failure still returns 202 once
the PostgreSQL record has committed; reconciliation subsequently delivers it.

## Handler contract

Implement `IJobHandler` from
[`app/application/ports/jobs.py`](../app/application/ports/jobs.py):

- `operation_type`: a stable registered name; never a Python import path supplied
  by a client.
- `validate(payload)`: reject invalid input with `InvariantViolationError` before
  it is committed. Validate again when executing persisted input.
- `async run(context)`: return a JSON object. Await `context.step(name, action)`
  for each resumable unit of work. The context exposes `job_id`, `user_id`,
  `correlation_id` and a copy of the input `payload`.

The shipped [diagnostic handler](../app/application/jobs/diagnostic.py) is a
minimal working example. Register production handlers in
[`build_job_runtime`](../app/core/container.py), which owns construction and
dependency injection. Deploy the same registry to producers and workers. Tests
can pass an explicit `handlers` iterable to that factory.

Await steps sequentially and give each a unique, stable name, such as
`parse-document-v1:<document-id>`. A completed step returns its saved JSON object
without calling the action again. Do not reuse a name for different work or
rename persisted steps during an in-flight deployment. Values must be JSON
objects/lists/primitives with string object keys and finite numbers. The default
limits are 64 KiB input and 1 MiB total checkpoint/result data, configurable via
`QUEUE__MAX_PAYLOAD_BYTES` and `QUEUE__MAX_CHECKPOINT_BYTES`.

The effect and checkpoint are two separate operations. If the process dies
after an effect but before its checkpoint commits, that action can run again.
Use domain idempotency or a transaction for such effects. Ticket #22 owns
canonical-input request deduplication; the current `idempotency_key` stores a
fresh job UUID and does not deduplicate submissions.

Raise `JobPaused` only after persisting an intentional wait. It releases the
worker and leaves the logical job STARTED; it does not introduce a workflow
state into the T7 lifecycle. Future workflow handlers must verify persisted
approval before performing finalization, including on every redelivery.
`JobCancelled` records CANCELLED, and the context checks the persisted
cancellation flag between steps. Setting that flag through an API/Redis transport
belongs to #21. Other handler exceptions record FAILED with a safe code;
storage failures leave STARTED for recovery.

## Recovery from Redis loss or worker death

The lifecycle is exactly:

```text
PENDING → QUEUED → STARTED → COMPLETED | FAILED | CANCELLED
```

The database rejects illegal transitions through the store contract. Progress,
approval waits and retry delays are not lifecycle states. Failed/terminal jobs
cannot be reopened in #20; the retry policy and endpoints belong to #22.

| Persisted state | Recovery operation |
| --- | --- |
| PENDING | `reconcile` commits QUEUED before publishing |
| QUEUED | `reconcile` republishes the existing UUID |
| STARTED after confirmed worker interruption | `resume <UUID>` republishes it without changing state/checkpoints |
| STARTED while deliberately awaiting approval | Leave it waiting; the authorized workflow command resumes it |
| COMPLETED / FAILED / CANCELLED | No automatic re-execution |

```bash
python -m app.core.jobs_cli reconcile --limit 100
python -m app.core.jobs_cli resume <UUID>
```

For Compose, run `docker compose --profile jobs exec worker` followed by the
same Python command. Reconciliation processes at most 100 rows by default
(maximum 1000); repeat as workers drain those rows. It can publish duplicates.
The runner skips terminal jobs and excludes concurrent execution using a
PostgreSQL advisory lock. This lock requires a direct or session-pooled database
connection and holds one connection per executing job.

Redis visibility timeout can also redeliver unacknowledged messages; it is not
the authoritative recovery mechanism. There is no heartbeat scanner or automatic
recovery scheduler in this ticket. An operator must distinguish a crash from an
intentional approval wait before using `resume`. Ticket #22 supplies the
phase-aware policy, scheduling, retry/backoff and additional concurrency controls.

## Verification

Point these environment variables at **disposable** local PostgreSQL + pgvector
and Redis services:

- `T20_TEST_DATABASE_URL`: a `postgresql+psycopg://` URL.
- `T20_TEST_REDIS_URL`: a `redis://` URL.
- `TEST_DATABASE_URL`: a plain `postgresql://` admin URL to also run Ticket #5's
  existing durable-ownership tests.

```bash
pytest tests/integration/test_job_queue.py tests/integration/test_jobs_api.py -v
```

Tests create unique PostgreSQL schemas and Redis queues. They never flush the
whole Redis service, and migration version tables belong to the test schema.
CI provisions PostgreSQL/Redis; missing T20 service configuration fails there
instead of silently skipping. The suite covers real HTTP 202 and authenticated
polling, broker loss, unavailable publishing, hard worker death, resume without
repeating committed effects, safe failures, intentional pauses, execution locks,
illegal transitions, and migration downgrade/reapply.

The ADR is [ADR-004](./adr/ADR-004-async-job-execution.md). SSE/cancel transport
(#21), idempotency/recovery policy (#22) and per-domain handlers (#8/#12/#17)
remain separate deliverables.
