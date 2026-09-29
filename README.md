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

## Documentation

- [BRD](docs/BRD.md) — business requirements and the traceability matrix
- [System Design](docs/SYSTEM-DESIGN.md) — authoritative architecture and data flows
- [Architecture Map](docs/ARCHITECTURE.md) — where new code goes, per-layer rules
- [ADRs](docs/adr/) — architecture decision records
- [Security](docs/SECURITY.md) · [Evaluation](docs/EVALUATION.md)
