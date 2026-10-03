# Implementation Architecture Map

This document provides a concise developer-oriented map of the Domain Copilot implementation.

For the complete architectural baseline, target architecture, implemented architecture, trust boundaries, data flows, and architectural decisions, refer to [`SYSTEM-DESIGN.md`](./SYSTEM-DESIGN.md).

`SYSTEM-DESIGN.md` is the authoritative architecture document.

---

## 1. Project Structure

The implementation follows Clean/Hexagonal Architecture with dependencies directed inward.

```text
app/
  domain/          Enterprise business rules: entities, value objects, domain
                   errors. Pure standard library -- no app.* layer, no third-party.
  application/     Use cases, ports (typing.Protocol), commands/DTOs. Imports only
                   app.domain -- no framework, ORM, or SDK; no pydantic.
  infrastructure/  Adapters implementing the application ports. SDKs live here
                   (persistence, LLM/embedding providers, vector store, queue).
  presentation/    FastAPI app, routes, pydantic request/response schemas, SSE,
                   and Depends() wiring. Maps domain errors to HTTP status codes.
  core/            Configuration and the composition root (container.py) that
                   constructs adapters and injects them into use cases.
```

**Naming note (reconciled with the frozen SDD).** `SYSTEM-DESIGN.md` describes
the layers using the conceptual names `adapters/`, `api/`, and `workers/`. The
implementation folds these into the tree above without changing dependency
direction:

| SDD conceptual name | Implemented location |
| ------------------- | -------------------- |
| `adapters/` (interface adapters) | `app/infrastructure/*` (driven adapters) and `app/presentation/*` (driving adapters) |
| `api/` (FastAPI app, routes, schemas, SSE) | `app/presentation/api/*` |
| `workers/` (Celery/T7 background execution) | `app/core/worker.py` entrypoint; `app/infrastructure/queue/` adapter; `app/application/jobs/` runner |

The exact directory structure may evolve during implementation, but dependency
direction and architectural boundaries must remain intact, and are enforced
automatically (see §1.2).

### 1.1 Where does new code go?

| Kind of code | Layer | Path |
| ------------ | ----- | ---- |
| Entity, value object, domain error, invariant | `domain` | `app/domain/<context>/` |
| Use case, port (interface), command, DTO | `application` | `app/application/<context>/`, ports in `app/application/ports/` |
| SDK / DB / LLM / queue adapter implementing a port | `infrastructure` | `app/infrastructure/<concern>/` |
| HTTP route, pydantic schema, `Depends()` provider | `presentation` | `app/presentation/api/` |
| Settings, dependency wiring (composition root) | `core` | `app/core/` |

Rule of thumb: **an SDK import belongs only in `infrastructure` (or `presentation`
for the web framework).** If business logic needs a capability, it depends on a
**port** in `app/application/ports/`; the concrete adapter is wired in
`app/core/container.py`.

### 1.2 Enforcement

The per-layer import bans (SDD §A.3.2) are enforced two complementary ways:

- **import-linter** contracts in `pyproject.toml` (`[tool.importlinter]`), run as
  the `lint-imports` step in CI.
- an **AST boundary test** (`tests/architecture/test_boundaries.py`) run in the
  normal `pytest` step.

Both must pass before AR-1 is considered implemented. The rationale is recorded
in [`adr/ADR-005-clean-hexagonal-boundary-enforcement.md`](./adr/ADR-005-clean-hexagonal-boundary-enforcement.md).

### 1.3 Representative request flow (`RegisterDocument`)

The first implemented vertical slice registers a document's metadata prior to
async ingestion. It exercises three ports and proves the layering end to end:

```text
POST /api/v1/documents
   │  app/presentation/api/routes/documents.py      (route + pydantic schema)
   ▼
Depends(get_register_document_use_case)
   │  app/presentation/api/dependencies.py
   ▼
Container.register_document_use_case()
   │  app/core/container.py                          (composition root; only
   │                                                   module importing infra)
   ▼
RegisterDocumentUseCase.execute(command)
   │  app/application/documents/use_cases.py         (depends on ports only)
   ▼
IDocumentRepository  (port)                          app/application/ports/
   ▼
InMemoryDocumentRepository  (adapter)                app/infrastructure/persistence/
```

Registration is **idempotent by `content_hash`**: a new hash creates and persists
a `Document` (HTTP `201`); an existing hash returns the existing document without
creating a duplicate or overwriting its metadata (HTTP `200`). This registration
slice remains in memory. Durable ingestion is the separate
`POST /api/v1/documents/ingest` route implemented in Ticket #8 (§6); metadata
registration does not enqueue or index a document.

### 1.4 Configuration (AR-4)

Configuration is externalised via `pydantic-settings` in
[`app/core/config.py`](../app/core/config.py) — the only place pydantic-settings
appears (an edge concern). `Settings` composes nested groups (`llm`, `embedding`,
`queue`, `database`, `auth`, `retrieval`, `ingestion`, `limits`, `retry`, `prompts`) populated with
the `__` nested delimiter, so `LLM__MODEL` sets `settings.llm.model`. Jobs require
`DATABASE__URL`; production also requires the configured JWT secret. **Secrets never live
in code or git (C6):** API keys are `SecretStr | None = None`, read from the
environment at runtime and masked in logs/`repr`. `.env.example` documents every
variable with blank secret values. Connection URLs are excluded from settings
`repr`. Provider and queue adapters consume these settings through the container.

### 1.5 Prompts (AR-4)

Product prompts are **versioned artifacts, never inline literals**. The
application depends on the `IPromptProvider` port + framework-free `Prompt` value
([`app/application/ports/prompts.py`](../app/application/ports/prompts.py)); the
YAML loader ([`app/infrastructure/prompts/yaml_prompt_provider.py`](../app/infrastructure/prompts/yaml_prompt_provider.py))
is the only module importing a YAML parser. Artifacts live in `prompts/*.yaml`,
each declaring its own `id`/`version`; the loader validates the schema eagerly at
startup (a malformed prompt fails boot) and resolves versions deterministically —
`get(id)` returns the highest version, `get(id, version=n)` returns exactly `n`.

### 1.6 Error handling at the boundary (AR-5; SDD §A.5.1)

Use cases raise typed domain/application errors and never touch `HTTPException`.
[`app/presentation/api/errors.py`](../app/presentation/api/errors.py) is the
single place that maps errors *by type* to a stable body `{"detail", "code"}`.
Responses split by fault: **client faults** (422/409/404/400) carry the error's
own message; **server faults** — `ConfigurationError` and any unmapped
`Exception` (both 500) — carry a fixed generic message while the real cause is
logged server-side and never returned, so internal detail cannot leak.

### 1.7 DI conventions (AR-3)

The composition root ([`app/core/container.py`](../app/core/container.py)) is the
only adapter-construction site; it builds adapters (and the prompt provider) from
config and injects them into use cases by **constructor injection**. FastAPI
`Depends()` bridges request scope to the container via
[`app/presentation/api/dependencies.py`](../app/presentation/api/dependencies.py).
Presentation never instantiates an adapter directly. This is implemented for the
current application surface; future adapters are wired here as their tickets land.
The rationale for §§1.4–1.7 is recorded in
[`adr/ADR-006-configuration-prompts-and-error-model.md`](./adr/ADR-006-configuration-prompts-and-error-model.md).

---

## 2. Dependency Direction

Dependencies point inward toward the domain:

```text
domain
  ↑
application
  ↑
adapters / infrastructure
```

Or, more explicitly:

```text
                ┌──────────────────────┐
                │      application     │
                │   defines use cases  │
                │      + ports         │
                └──────────▲───────────┘
                           │
                ┌──────────┴───────────┐
                │     infrastructure   │
                │ implements the ports │
                └──────────────────────┘
```

The `domain` layer must not depend on:

* FastAPI
* Celery
* Redis
* PostgreSQL
* pgvector
* LLM SDKs
* embedding SDKs
* vector database SDKs
* web frameworks
* infrastructure-specific implementations

The application depends on abstractions/ports rather than concrete external providers.

---

## 3. Provider Boundaries

External capabilities are accessed through explicit interfaces.

### LLM

```text
application/domain
        │
        ▼
   ILLMProvider
        │
        ├── Groq adapter
        └── Ollama adapter
```

Additional providers such as Gemini may be added through their own adapter where configured.

### Embeddings

Embeddings are intentionally separated from the LLM abstraction:

```text
application/domain
        │
        ▼
IEmbeddingProvider
        │
        └── Local sentence-transformers adapter
```

### Vector / Retrieval Storage

Retrieval infrastructure is accessed through application-level ports rather than exposing pgvector/PostgreSQL implementation details to domain logic.

This separation allows provider implementations to be replaced without changing core business rules.

---

## 4. Database Ownership

PostgreSQL is the **authoritative durable state store** for the assessment implementation.

Logical domain components own their persistence boundaries and must not bypass application/domain rules through arbitrary cross-domain database access.

The implementation does not require independent databases for every logical component.

For Part B, the system uses a single PostgreSQL deployment with logically separated tables/schema ownership.

Ticket #6's declarative mappings live in
[`app/infrastructure/persistence/models.py`](../app/infrastructure/persistence/models.py).
They mirror the migrations through `95c7e8a12d40`, including Ticket #5
ownership/session fields, Ticket #20 job fields, Ticket #9's separate
`chunk_embeddings` table and Ticket #8's document source/artifact storage.
`Base.metadata` is shared by Alembic, the job store, retrieval store and ingestion
store. There is one migration chain and no independent table declarations.
Ticket #8's additive revision follows `4c1e9a7d52b8` and preserves prior documents,
chunks, vectors and indexes.
Ticket #5's user and ownership adapters retain their bound SQL against the same
schema; ORM objects do not cross application or domain boundaries.

Migrations remain the schema authority. After `alembic upgrade head`,
`alembic check` must report no pending operations. Changes to mapped columns,
defaults, constraints or indexes must accompany a migration. The real PostgreSQL
tests in `tests/integration/test_orm_models.py` compare the schema and server
defaults, exercise Alembic's configured metadata, and read runner-written jobs
through the ORM and Ticket #5 ownership query.

Redis is infrastructure for:

* Celery brokering
* ephemeral cancellation signaling
* rate limiting where configured
* transient pub/sub/infrastructure concerns

Redis is **not authoritative** for durable jobs, workflow state, audit history, or approval state.

---

## 5. Job and Workflow Lifecycle

Job lifecycle and clinical workflow state are intentionally separate.

### T7 Job Lifecycle

```text
PENDING
   ↓
QUEUED
   ↓
STARTED
   ├──────────────► COMPLETED
   ├──────────────► FAILED
   └──────────────► CANCELLED
```

PostgreSQL is authoritative for lifecycle state.

Celery/Redis provides asynchronous execution infrastructure.

### Clinical Workflow Lifecycle

```text
RESEARCH
   ↓
SAFETY_CHECK
   ↓
DRAFT
   ↓
AWAITING_APPROVAL
   ├──► REJECTED
   │
   └──► FINALIZE
          ↓
      COMPLETED
```

The worker does not hold a worker slot while waiting for human approval.

At `AWAITING_APPROVAL`:

1. workflow state is checkpointed in PostgreSQL,
2. an `awaiting_approval` event is persisted,
3. the worker task returns,
4. the approval command records the reviewer decision,
5. approval enqueues/resumes finalization,
6. a worker continues from the persisted checkpoint.

A rejected clinical note is a terminal workflow outcome. The underlying T7 job can become `COMPLETED` because the requested workflow was successfully processed and the reviewer decision was recorded.

---

## 6. RAG Data Flow

### Ingestion (Ticket #8, implemented)

```text
Document Upload
      ↓
API
      ↓
T7 Job
      ↓
Celery Worker
      ↓
Extract
      ↓
Clean
      ↓
Structure-Aware Chunk
      ↓
IEmbeddingProvider
      ↓
pgvector + PostgreSQL FTS
```

Embeddings are generated through the embedding provider abstraction, **not through the LLM provider abstraction**.

Document metadata and ingestion state are persisted in PostgreSQL.

`POST /api/v1/documents/ingest` authorizes the admin ingestion capability and
accepts a bounded raw PDF/Markdown body. `IngestionService` computes source
identity and prepares a T7 job. `PostgresIngestionStore.accept` commits document,
original bytes and PENDING job together; `JobService.dispatch` then queues it.
The response is HTTP 202 with job/document polling URLs.

| Concern | Implementation |
| --- | --- |
| Source/stage/chunk values and deterministic IDs | `app/domain/documents/ingestion.py` |
| Ingestion store, extractor and tokenizer ports | `app/application/ports/ingestion.py` |
| Submission and resumable job handler | `app/application/documents/ingestion_service.py`, `ingestion_handler.py` |
| Structure-aware windows and complete embedding coverage | `app/application/documents/chunking.py`, `embedding.py` |
| PDF/CommonMark parsing | `app/infrastructure/ingestion/extractors.py` |
| Durable bytes, artifacts and progress | `app/infrastructure/persistence/sql/ingestion_store.py` |
| Dense/keyword indexing | Ticket #9's `IRetrievalStore` / `PostgresRetrievalStore` |
| HTTP upload and document polling | `app/presentation/api/routes/documents.py` |
| Synthetic seed and file-upload CLI | `scripts/ingest_documents.py` |

The default windows are 512 tokenizer tokens with 64 overlap within the same
page/heading hierarchy. The tokenizer port is supplied by the local embedding
adapter. For a model whose context is smaller, every model-sized subwindow is
embedded and combined by token weight; no tail is silently truncated. Citation
records include document version and ingestion timestamp alongside the existing
document/name/page/section/chunk fields and embedding provenance.

Each stage and embedding/index batch persists a PostgreSQL artifact before its
T7 checkpoint. Checkpoints contain references rather than source text/vectors.
Replays reuse artifacts or repeat deterministic index upserts. Duplicate source
acceptance shares an active/completed job; a failed/cancelled source gets a new
attempt while retaining completed artifacts and the old terminal job.

The API borrows the existing database pool. Worker async sessions use
`NullPool` because Celery invokes a separate `asyncio.run` per task. Model loading
is lazy. The default Compose stack includes migrations, API, worker, PostgreSQL
and Redis; `docker compose run --rm --build seed` exercises both supported formats.

See [INGESTION.md](./INGESTION.md) for setup, source/version identity, limits and
recovery, and [ADR-001](./adr/ADR-001-chunking-and-ingestion-embeddings.md) for
chunking and embedding tradeoffs. SSE/cancel transport remains #21 and generic
recovery/idempotency policy remains #22.

---

### Retrieval

Ticket #9 implements independent dense/keyword search with citation metadata.
Ticket #10 adds application-layer RRF (k=60), the local BGE cross-encoder and
evidence filtering in the following flow, reusing #7's embedding/chat providers.

```text
User Query
    ↓
Application
    ↓
Query Embedding
    ↓
┌───────────────────────┐
│ Dense pgvector        │
│ PostgreSQL FTS        │
└──────────┬────────────┘
           ↓
      RRF Fusion
        k = 60
           ↓
   Cross-Encoder Reranker
           ↓
    Evidence Threshold
           ↓
   Retrieved Chunks
           ↓
   Citation Objects
```

When evidence is insufficient, grounded ask returns exactly:

> Not enough information in the corpus

`POST /retrieve` and `POST /ask` require `ASK_QUESTION`. The new `IReranker` port
keeps model imports in infrastructure. Grounded-answer v2 selects source IDs;
the application returns complete indexed excerpts and the exact seven citation
fields. `relevance_score` is the sigmoid-normalized BGE logit. No clinical prose
or source metadata is accepted from the model.

`RetrievalObserver` uses the existing audit port and logging sink. The
PostgreSQL sink writes #6's existing trace/span models through the shared
session factory, preserving identity, counts, scores, selected chunks, latency
and refusal. No schema or job infrastructure changes are required.

See [RETRIEVAL.md](./RETRIEVAL.md) and
[ADR-008](./adr/ADR-008-hybrid-retrieval-and-grounded-qa.md) for score semantics,
resource limits, verification and the conservative grounding policy.

---

## 7. Agent and Tool Boundaries

Agents operate through explicit application-level contracts and least-privilege tool access.

```text
                    Orchestrator
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
 Guideline           Safety           Documentation
 Researcher          Checker             Drafter
        │                │                │
 search_corpus      check_interactions  draft_clinical_note
 retrieve_drug_info validate_dosage
```

The orchestrator controls the workflow order.

Agents:

* receive typed inputs,
* return typed outputs,
* have explicit tool allowlists,
* have bounded iterations,
* cannot modify protected state directly,
* cannot bypass authorization,
* cannot bypass the human approval gate.

Retrieved documents and user-provided content are treated as **untrusted data**, not executable instructions.

---

## 8. Human Approval Boundary

Clinical note finalization has an explicit authorization boundary:

```text
Research
   ↓
Safety Check
   ↓
Draft
   ↓
Human Review
   │
   ├── Reject
   │
   └── Approve
          ↓
      Finalize
```

`finalize_clinical_note` is a gated write operation.

The finalization operation must verify persisted approval rather than trusting a client-provided flag such as:

```text
approved=true
```

The reviewer decision and resulting finalization are auditable.

---

## 9. Async Event Flow

### Implemented in Ticket #20

`POST /api/v1/jobs` authenticates with Ticket #5's JWT/permission dependencies,
persists PENDING then QUEUED, publishes a UUID, and returns HTTP 202 with its
polling URL. `GET /api/v1/jobs/{job_id}` enforces PostgreSQL ownership and returns
state/result/error. Generic submissions require admin permission. Ticket #8's
ingestion route applies its own admin capability and commits its source and job
atomically; future #12/#17 routes must apply their own domain authorization.

| Concern | Implementation |
| --- | --- |
| Exact lifecycle | `app/domain/jobs/entities.py` |
| Store/queue/handler contracts | `app/application/ports/jobs.py`, `queue.py` |
| Submission, checkpoint runner, registry | `app/application/jobs/` |
| Durable rows and execution locks | `app/infrastructure/persistence/job_store.py` |
| Celery UUID transport | `app/infrastructure/queue/celery_queue.py` |
| Wiring and worker | `app/core/container.py`, `app/core/worker.py` |
| Explicit reconciliation/resume | `python -m app.core.jobs_cli` |

Redis results are disabled. PostgreSQL owns all job state and checkpoints.
Workers skip committed named steps and terminal deliveries, with a PostgreSQL
session lock excluding concurrent execution. A deliberate `JobPaused` releases
the worker while retaining STARTED; reconciliation selects only PENDING/QUEUED.
See [JOBS.md](./JOBS.md) and [ADR-004](./adr/ADR-004-async-job-execution.md) for
the at-least-once effect boundary and the work reserved for #21/#22.

### Target progress flow (#21)

The following durable event/SSE flow is the design for the streaming ticket;
Tickets #20/#8 implement submission, execution and progress polling; they do not
publish this target event stream.

```text
Client
  │
  │ POST /jobs
  ▼
FastAPI
  │
  ├──► PostgreSQL: create job
  │
  └──► Redis/Celery: enqueue
                │
                ▼
             Worker
                │
                ├──► PostgreSQL: state/checkpoint
                │
                └──► PostgreSQL: job_events
                              │
                              ▼
                         SSE endpoint
                              │
                              ▼
                            Client
```

SSE is a progress/read channel.

Disconnecting the SSE client does **not** cancel or modify the underlying job.

Cancellation is performed explicitly through the job cancellation API.

---

## 10. Observability Boundary

A correlation ID connects:

```text
Request
   ↓
Job
   ↓
Workflow
   ↓
Agent Run
   ↓
Tool Call
   ↓
LLM Call
```

The custom PostgreSQL trace store records relevant spans, inputs/outputs subject to security controls, duration, status, token usage, and cost information.

This allows an evaluator to inspect a complete run using its run/correlation ID.

---

## 11. Implementation Principle

The implementation should preserve the boundaries defined by the frozen SDD without prematurely implementing the full production target architecture.

Part B is intentionally an assessment MVP:

```text
FastAPI
   │
   ├── PostgreSQL + pgvector
   ├── Redis
   ├── Celery Worker
   ├── LLM Provider Adapters
   ├── Embedding Provider
   └── Reranker
```

The production-scale components described in the target architecture remain architectural extension points rather than mandatory assessment-MVP infrastructure.

Any architectural change that materially affects the frozen baseline must be documented through an ADR rather than silently changing the design.
