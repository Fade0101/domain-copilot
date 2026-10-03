# Domain Copilot

A healthcare documentation copilot built on retrieval-augmented generation and a
multi-agent workflow, with durable long-running (async) jobs. This repository is
an ITI take-home assessment implementation.

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

Tickets #8/#20 provide PDF/Markdown ingestion on a real Celery/Redis worker,
with PostgreSQL-owned source files, stage progress, checkpoints and job results.
Copy `.env.example` to `.env`, choose a local `POSTGRES_PASSWORD`, configure
`DATABASE__URL` using host `postgres`, and supply `AUTH__SECRET_KEY` (32+
characters) and a development `AUTH__DEMO_PASSWORD` (12+ characters). Then:

```bash
docker compose up --build -d
docker compose run --rm --build seed
```

The default stack starts migrations, API, worker, PostgreSQL/pgvector and Redis.
The seed command ingests synthetic PDF and Markdown examples and waits for them
to become searchable. Repeating it reuses the same document/job IDs and chunks.
The first ingestion downloads the public local embedding model; its cache is
retained in a Docker volume. No chat API key is needed for ingestion.

Open `http://localhost:8000/docs`, authenticate as the configured admin, and use
`POST /api/v1/documents/ingest` with raw file bytes. It returns HTTP 202 and job/
document polling URLs. See [Document Ingestion](docs/INGESTION.md) for upload
examples, 512/64 chunking, citation metadata, limits and retry/resume behavior.

The generic job smoke operation is
`{"operation_type":"diagnostic","payload":{}}` at `POST /api/v1/jobs`.
[Async Jobs](docs/JOBS.md) documents native setup, handler registration and
explicit recovery commands. SSE/cancel transport (#21), general recovery policy
(#22), retrieval/fusion (#10) and corpus content (#11) remain separate work.

## Documentation

- [BRD](docs/BRD.md) — business requirements and the traceability matrix
- [System Design](docs/SYSTEM-DESIGN.md) — authoritative architecture and data flows
- [Architecture Map](docs/ARCHITECTURE.md) — where new code goes, per-layer rules
- [ADRs](docs/adr/) — architecture decision records
- [Security](docs/SECURITY.md) · [Evaluation](docs/EVALUATION.md)
- [Async Jobs](docs/JOBS.md) — startup, API, checkpoint and recovery contracts
- [Document Ingestion](docs/INGESTION.md) — PDF/Markdown uploads, synthetic seed, citations and recovery
