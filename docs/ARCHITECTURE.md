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
| `workers/` (Celery/T7 background execution) | `app/infrastructure/*` (future ticket) |

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
creating a duplicate or overwriting its metadata (HTTP `200`). The in-memory
adapter is interim; a SQLAlchemy/pgvector adapter replaces it behind the same
port in a later ticket.

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

### Ingestion

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

---

### Retrieval

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

When evidence does not meet the required threshold, the system may return:

> Not enough information in the corpus.

The retrieval pipeline must not manufacture evidence to satisfy a query.

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

Long-running jobs communicate progress through durable job events.

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
