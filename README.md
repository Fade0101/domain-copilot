# Domain Copilot

A healthcare documentation copilot built on retrieval-augmented generation and a
multi-agent workflow, with durable long-running (async) jobs. This repository is
an ITI take-home assessment implementation.

## Reviewer quickstart

You need **Docker Desktop/Engine with Compose**, an internet connection, and a
**Groq API key** for answers and clinical workflows. Python, Node, PostgreSQL,
Redis and Ollama do not need to be installed on the host. Allow Docker about
8 GB of memory for the local embedding/reranking models.

From the repository root, generate local development credentials once (the
command works in PowerShell and Bash):

```bash
docker run --rm -v "${PWD}:/workspace" -w /workspace python:3.11-slim python scripts/configure_demo.py
```

Open the generated `.env` and set **`LLM__API_KEY`** to your Groq key. The helper
already sets the database connection, random passwords and signing key. Keep
`LLM__FALLBACK=` empty for this setup. Existing `.env` files are preserved;
if reusing one, check its database, signing key and demo-password settings.

Start the complete application:

```bash
docker compose up --build
```

Open **http://localhost:8000/**. Sign in as `analyst@example.com`,
`reviewer@example.com`, or `admin@example.com` using **`AUTH__DEMO_PASSWORD`**
from your local `.env`. Keep `.env` private.

Compose starts PostgreSQL/pgvector, Redis, database migrations, the worker and
the API/web UI, then automatically ingests five small synthetic demo documents.
Wait for **`seed` to exit with code 0** before asking about the corpus. Successful
`migrate` and `seed` containers remain exited; that is normal.

Try: **“According to the Kestrel training policy, what must a minimum
documentation record contain?”** Expand the citations, then follow
[the UI walkthrough](docs/WEB-UI.md) for jobs, approvals, final notes and traces.

The first build downloads Python packages; the first ingestion/question downloads
the local embedding/reranking models. Prepare this once before a live demo.
Subsequent starts reuse the image, model cache and database. This setup uses Groq
for chat and does not download an Ollama chat model.

## Assessment Variant: D0T7

| Axis | Formula | Computation | Result |
| ---- | ------- | ----------- | ------ |
| Domain | (last two National ID digits) mod 7 | `91 mod 7 = 0` | **D0 — Healthcare** |
| Twist | (sum of all National ID digits) mod 8 | `31 mod 8 = 7` | **T7 — Async Long-Running Jobs** |

## Architecture

Clean / Hexagonal (Ports & Adapters). Business and application logic are kept
independent of FastAPI, the ORM, the vector store, and LLM SDKs; the layer
boundaries are enforced automatically in CI (import-linter contracts + an AST
boundary test).

```text
app/
  domain/          entities, value objects, domain errors (pure standard library)
  application/     use cases, ports (typing.Protocol), commands/DTOs
  infrastructure/  adapters implementing the ports (SDKs live here)
  presentation/    FastAPI routes, request/response schemas, DI wiring
  core/            configuration + composition root (dependency wiring)
```

[`docs/SYSTEM-DESIGN.md`](docs/SYSTEM-DESIGN.md) is the authoritative architecture
document; [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the developer-oriented
map, and decisions are recorded in [`docs/adr/`](docs/adr/).

## Tech Stack

Python 3.11 · FastAPI · PostgreSQL 16 + pgvector · Redis · Celery ·
sentence-transformers · pydantic v2

## Development

Install dependencies and run the quality gate:

```bash
pip install -r requirements.txt

ruff check .
ruff format --check .
mypy .
lint-imports
pytest
```

Run the API locally:

```bash
uvicorn app.presentation.api.app:create_app --factory --reload
```

## Database migrations

Configure `DATABASE__URL` in your environment or `.env`, then run:

```bash
alembic upgrade head
alembic check
```

Ticket #6's [ORM models](app/infrastructure/persistence/models.py) mirror the
migrations and supply the job runner's table mapping. `alembic check` should
report no pending operations after upgrading. Update the models alongside any
new migration. Schema agreement and integration with Tickets #5/#20 are tested
in `tests/integration/test_orm_models.py`; set `TEST_DATABASE_URL` to an admin
connection on a disposable PostgreSQL service to run its database tests.

## Run with Docker

Use the [reviewer quickstart](#reviewer-quickstart) for first-time setup. To run
in the background and inspect startup:

```bash
docker compose up --build -d
docker compose ps -a
docker compose logs --tail=40 seed
```

The application services share one `domain-copilot:local` image. PostgreSQL,
Redis and model-cache volumes preserve data across restarts. The automatic seed
includes two PDF/Markdown smoke examples plus the synthetic minimum-record,
allergy-status and closed-loop handoff policies. Seeding is idempotent; to rerun
it explicitly, use `docker compose run --rm seed`. No chat API key is needed
for ingestion, but one is required for Groq-backed Q&A/workflows.

| Check | Expected result / next step |
| --- | --- |
| `http://localhost:8000/health` | HTTP 200: API is alive. |
| `http://localhost:8000/ready` | HTTP 200 when PostgreSQL, Redis, embeddings and Groq are available. A first model load can temporarily return 503; retry after it finishes. |
| `seed` exits nonzero | Read `docker compose logs --tail=80 seed worker`; rerun seed after fixing the reported problem. |
| Groq is unavailable | Check `LLM__API_KEY`, internet access and quota. Keep `LLM__FALLBACK=` empty unless Ollama is configured. After changing `.env`, run `docker compose up -d` again. |
| A port is occupied | Stop the previous application stack or adjust the host port in Compose; the API defaults to 8000, PostgreSQL to 5432, Redis to 6379. |

Stop with `docker compose down`; start again with `docker compose up -d`.
Do not add `-v` when stopping: that deletes the saved data and model cache.

For the full versioned evidence corpus, after startup run
`docker compose exec api python scripts/corpus.py ingest --api-url http://api:8000 --output-directory /tmp/corpus-build`.
This optional import takes longer than the small default demo corpus.

Open **http://localhost:8000/** for the web workspace. Sign in with a seeded
`analyst@example.com`, `reviewer@example.com`, or `admin@example.com` account and
your configured demo password. The UI is included in the API image; no Node
server or separate frontend build is needed. See [Web UI](docs/WEB-UI.md) for
the conversation → workflow → review → finalized-note walkthrough.

The API explorer remains at `http://localhost:8000/docs`. Authenticate as the configured admin and use
`POST /api/v1/documents/ingest` with raw file bytes. It returns HTTP 202 and job/
document polling URLs. See [Document Ingestion](docs/INGESTION.md) for upload
examples, 512/64 chunking, citation metadata, limits and retry/resume behavior.

The generic job smoke operation is
`{"operation_type":"diagnostic","payload":{}}` at `POST /api/v1/jobs`.
[Async Jobs](docs/JOBS.md) documents native setup, handler registration and
explicit recovery commands. [SSE, progress and cancellation](docs/JOB_STREAMING.md)
(#21) add PostgreSQL-backed replay and opt-in token generation. SSE disconnect
leaves the job running. Durable recovery (#22) and the clinical workflow (#17)
power the web workspace's progress and approval journey.

## Hybrid retrieval and grounded Q&A

Ticket #10 adds authenticated `POST /api/v1/retrieve` (`{"query":"..."}`) and
`POST /api/v1/ask` (`{"question":"..."}`). Retrieval combines the existing dense
and keyword searches using RRF with k=60, then locally reranks with
`BAAI/bge-reranker-v2-m3`. The first retrieval downloads the model weights.

Grounded ask uses the configured #7 chat provider to select evidence, returns
complete indexed excerpts with structured citations, and refuses insufficient
evidence with exactly `Not enough information in the corpus`. Supply chat
credentials through the existing `LLM__*` settings. See
[Hybrid Retrieval and Grounded Q&A](docs/RETRIEVAL.md) for requests, score meaning,
model resources, traces and real-model verification.

## Human approval

Ticket #19 adds review, approve, reject-with-reason and edit-and-approve endpoints
under `/api/v1/runs/{workflow_id}/approval`. Reviewer/admin permissions are
enforced on the server. Original drafts, safety provenance, decisions and edit
diffs are persisted; rejection completes the linked job. The #17 workflow
registers the #16 draft through the internal approval service. The web UI then
supports the human decision and explicit guarded finalization request. See
[Human Approval Gate](docs/APPROVALS.md) for that contract and the durable
finalization handoff. #19 does not run an orchestrator or finalize notes directly.

## Observability

Ticket #23 adds durable traces for synchronous asks and async execution, including
retrieval, agents, tools and provider calls. Send a UUID in `X-Correlation-ID`, or
use the generated value returned in that response header. Query
`GET /api/v1/traces`, `GET /api/v1/traces/{id}/spans`, and `GET /api/v1/usage`
with your bearer token. Filters include run/job, correlation, user and time.
Ownership is enforced by the existing authorization rules.

Costs use explicit `OBSERVABILITY__RATES` configuration and are labelled estimates;
unknown prices or token counters remain unavailable. `/health` reports process
liveness. `/ready` probes PostgreSQL, Redis, local embeddings, and each configured
chat provider, returning 503 when a probe fails or times out. An unused fallback
should be disabled with `LLM__FALLBACK=`. See [Observability](docs/OBSERVABILITY.md)
for the query contract, rate configuration, readiness scope and accounting limits.

## Documentation

- [Web UI](docs/WEB-UI.md) — startup, full user journey, streaming recovery and browser tests
- [API Contracts](docs/API-CONTRACTS.md) and [published OpenAPI](docs/openapi.json) — endpoints,
  citations, refusals, typed errors, roles, ownership, SSE and session history

- [BRD](docs/BRD.md) — business requirements and the traceability matrix
- [System Design](docs/SYSTEM-DESIGN.md) — authoritative architecture and data flows
- [Architecture Map](docs/ARCHITECTURE.md) — where new code goes, per-layer rules
- [ADRs](docs/adr/) — architecture decision records
- [Security](docs/SECURITY.md) · [Evaluation](docs/EVALUATION.md)
- [Async Jobs](docs/JOBS.md) — startup, API, checkpoint and recovery contracts
- [Human Approval Gate](docs/APPROVALS.md) — persisted reviews, decisions, audit and #17 handoff
- [Document Ingestion](docs/INGESTION.md) — PDF/Markdown uploads, synthetic seed, citations and recovery
- [Hybrid Retrieval and Grounded Q&A](docs/RETRIEVAL.md) — RRF, BGE scores, citations and refusal
