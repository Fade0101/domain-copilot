# System Design Document (SDD)

## Domain Copilot — D0 Healthcare + T7 Async Long-Running Jobs

| Field              | Value                                                        |
| ------------------ | ------------------------------------------------------------ |
| **Document ID**    | SDD-DC-D0T7                                                  |
| **Version**        | 1.1                                                          |
| **Status**         | **FROZEN** 🔒                                                |
| **Author**         | Fady Ehab                                                    |
| **Date**           | 2026-09-27                                                   |
| **BRD Reference**  | BRD-DC-D0T7 v1.2 (FROZEN)                                   |
| **Variant**        | D0 Healthcare · T7 Async Long-Running Jobs                   |

> **Status: FROZEN.** This document is the architectural baseline for implementation. Changes after freeze require an explicit architectural decision recorded through an ADR and must not silently invalidate the BRD traceability baseline.

> This document has two parts. **Part A** describes the target architecture unconstrained by assessment time/budget. **Part B** describes the implemented MVP, with a gap table documenting every deliberate simplification, its interim mitigation, and the effort to close.
>
> **Part B's reasoning matters more than Part A's size.**

---

# Part A — Target Architecture (Unconstrained)

## A.1 System Context

Domain Copilot is an enterprise agentic RAG platform for healthcare organisations. It sits between clinical professionals and a managed document corpus, providing grounded clinical guidance through a multi-agent pipeline with mandatory human approval.

### A.1.1 External Actors

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         External Actors                                 │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────────────────┐ │
│  │ Clinician │  │ Reviewer │  │  Admin   │  │  LLM Provider APIs      │ │
│  │ (Analyst) │  │ (Senior) │  │          │  │  (Groq, Gemini, Ollama) │ │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └────────────┬────────────┘ │
│        │              │              │                    │              │
└────────┼──────────────┼──────────────┼────────────────────┼──────────────┘
         │              │              │                    │
         ▼              ▼              ▼                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       Domain Copilot Platform                           │
│                                                                         │
│  Ingests clinical documents · Answers with grounded citations          │
│  Executes clinical note workflow · Enforces human approval             │
│  Tracks all agent runs with full observability                         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### A.1.2 Key Integration Points

| Integration                | Direction     | Data Exchanged                                           | Trust Level          |
| -------------------------- | ------------- | -------------------------------------------------------- | -------------------- |
| Clinician / Reviewer / Admin | Inbound      | Queries, case summaries, approval decisions, documents   | Authenticated, trusted |
| LLM Provider (hosted)     | Outbound      | Prompts (with retrieved context), tool schemas           | Semi-trusted (data leaves infra) |
| LLM Provider (local/Ollama)| Internal      | Same as above, but data stays on-premises                | Trusted (no data egress) |
| Document Corpus (ingested) | Inbound       | Clinical PDFs, Markdown docs                             | **Untrusted** — content may contain injection payloads |

---

## A.2 Design Goals & Constraints

### A.2.1 Design Goals

| Priority | Goal                        | Rationale                                                                                  |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------ |
| G1       | **Clinical safety first**   | Hallucinated dosage/contraindication = patient harm. Refusal is always safer than inference. |
| G2       | **Grounded, never guessing**| Every clinical claim traceable to a source chunk. "Not enough information" is correct.      |
| G3       | **Human in the loop**       | No clinical note finalised without explicit human approval.                                 |
| G4       | **Full observability**      | Every run reconstructable: agents, tools, chunks, tokens, cost.                            |
| G5       | **Provider-agnostic**       | Swap LLM/embedding/vector provider = config + 1 adapter. No vendor lock-in.               |
| G6       | **Async-first (T7)**        | Long-running operations survive restarts, are resumable, cancellable, idempotent.          |
| G7       | **Teachable**               | Architecture explainable and defensible to post-graduate trainees.                          |

### A.2.2 Architectural Constraints

| Constraint | Description                                                                                           |
| ---------- | ----------------------------------------------------------------------------------------------------- |
| C1         | Domain and application layers must not import any LLM SDK, vector-store SDK, or web framework.        |
| C2         | Agent-accessible side-effecting tools that can modify clinical workflow artifacts require explicit human approval before execution. Internal persistence operations (job management, tracing, auditing, workflow state) are not considered agent side effects. |
| C3         | Retrieved document content is always untrusted data — never instructions.                             |
| C4         | PostgreSQL is the authoritative source of truth for all durable state.                                |
| C5         | No real patient data (PHI) in the system — synthetic/public data only.                                |
| C6         | No secrets in the repository or git history — ever.                                                    |
| C7         | Free-tier compatible — system must function with zero paid API subscriptions.                         |

---

## A.3 High-Level Architecture

### A.3.1 Target Production Architecture

```
                                    ┌─────────────────────────┐
                                    │     CDN / WAF / DDoS    │
                                    └────────────┬────────────┘
                                                 │
                                    ┌────────────▼────────────┐
                                    │     API Gateway          │
                                    │  (rate limiting, auth    │
                                    │   termination, routing)  │
                                    └────────────┬────────────┘
                                                 │
                    ┌────────────────────────────┼───────────────────────────┐
                    │                            │                           │
         ┌──────────▼──────────┐    ┌────────────▼────────────┐  ┌──────────▼──────────┐
         │   API Service       │    │   API Service            │  │   API Service        │
         │   (FastAPI)         │    │   (FastAPI)              │  │   (FastAPI)          │
         │   Instance 1        │    │   Instance 2             │  │   Instance N         │
         └──────────┬──────────┘    └────────────┬────────────┘  └──────────┬──────────┘
                    │                            │                           │
         ┌──────────▼────────────────────────────▼───────────────────────────▼──────────┐
         │                          Message Broker (Redis / RabbitMQ)                    │
         └──────────┬────────────────────────────┬───────────────────────────┬──────────┘
                    │                            │                           │
         ┌──────────▼──────────┐    ┌────────────▼────────────┐  ┌──────────▼──────────┐
         │   Celery Worker     │    │   Celery Worker          │  │   Celery Worker      │
         │   (Ingestion)       │    │   (Workflow/Agents)      │  │   (Evaluation)       │
         └─────────────────────┘    └─────────────────────────┘  └─────────────────────┘
                    │                            │                           │
         ┌──────────▼────────────────────────────▼───────────────────────────▼──────────┐
         │                                                                               │
         │   ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────────────┐ │
         │   │  Managed        │  │  Managed Redis   │  │   Object Storage (S3)        │ │
         │   │  PostgreSQL     │  │  (ElastiCache /  │  │   (document originals)       │ │
         │   │  + pgvector     │  │   Redis Cloud)   │  │                              │ │
         │   └─────────────────┘  └─────────────────┘  └──────────────────────────────┘ │
         │                                                                               │
         │   ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────────────┐ │
         │   │  Secrets Manager│  │  Centralized     │  │   Backup / DR               │ │
         │   │  (Vault / AWS   │  │  Observability   │  │   (automated pg_dump,       │ │
         │   │   Secrets Mgr)  │  │  (Grafana+Tempo+ │  │    cross-region replicas)   │ │
         │   │                 │  │   Prometheus)     │  │                              │ │
         │   └─────────────────┘  └─────────────────┘  └──────────────────────────────┘ │
         │                                                                               │
         └───────────────────────────────────────────────────────────────────────────────┘

                    │                            │
         ┌──────────▼──────────┐    ┌────────────▼────────────┐
         │  LLM Providers      │    │  Embedding Providers     │
         │  (Groq, Gemini,     │    │  (sentence-transformers, │
         │   Ollama cluster)   │    │   Gemini Embeddings)     │
         └─────────────────────┘    └─────────────────────────┘
```

### A.3.2 Dependency Direction (The Non-Negotiable Rule)

```
                ┌──────────────────────────────────────────┐
                │           Presentation Layer              │
                │   FastAPI routes · SSE endpoints           │
                │   Celery task definitions                  │
                │                                            │
                │   MAY import: Application, Domain          │
                │   MUST NOT: be imported by inner layers    │
                └──────────────────┬───────────────────────┘
                                   │ invokes
                                   ▼
                ┌──────────────────────────────────────────┐
                │           Application Layer               │
                │   Use cases · Service orchestration        │
                │   Port interfaces (ILLMProvider,           │
                │   IEmbeddingProvider, IDocumentRepo,       │
                │   IVectorStore, IJobQueue, etc.)           │
                │                                            │
                │   MAY import: Domain                       │
                │   MUST NOT import: any SDK, framework,     │
                │     or infrastructure concern              │
                └──────────────────┬───────────────────────┘
                                   │ depends on
                                   ▼
                ┌──────────────────────────────────────────┐
                │             Domain Layer                   │
                │   Entities · Value objects · Domain errors  │
                │   Business rules · Enums                   │
                │                                            │
                │   MAY import: nothing external              │
                │   MUST NOT import: anything outside domain  │
                │   Pure Python only — no third-party imports │
                └──────────────────────────────────────────┘
                                   ▲
                                   │ implements ports
                ┌──────────────────┴───────────────────────┐
                │         Infrastructure Layer              │
                │   Adapters implementing application ports  │
                │                                            │
                │   ┌─────────────┐ ┌─────────────────────┐ │
                │   │ LLM Adapters│ │ Embedding Adapters   │ │
                │   │ GroqAdapter │ │ SentenceTransformers │ │
                │   │ OllamaAdapt│ │ GeminiEmbedAdapter   │ │
                │   └─────────────┘ └─────────────────────┘ │
                │   ┌─────────────┐ ┌─────────────────────┐ │
                │   │ Persistence │ │ Job Queue            │ │
                │   │ SQLAlchemy  │ │ CeleryAdapter        │ │
                │   │ pgvector    │ │ Redis pub/sub        │ │
                │   └─────────────┘ └─────────────────────┘ │
                │                                            │
                │   MAY import: SDKs, frameworks, libraries  │
                │   MUST: implement application port ifaces  │
                └──────────────────────────────────────────┘
```

**The acceptance test:** swapping the LLM provider, embedding model, or vector store = configuration change + one new adapter file. Zero changes to domain or application code.

**What each layer must never import:**

| Layer          | Must Never Import                                                                |
| -------------- | -------------------------------------------------------------------------------- |
| Domain         | Any third-party library, any other layer                                         |
| Application    | `openai`, `groq`, `ollama`, `sqlalchemy`, `pgvector`, `fastapi`, `celery`, `redis` |
| Presentation   | Direct database access, direct LLM SDK calls (goes through application ports)     |
| Infrastructure | Nothing restricted — this is where all SDKs live                                 |

---

## A.4 Request / Execution Models

### A.4.1 Synchronous: Simple Q&A (Ask-with-Citations)

```
Client                    API                    Application              Infrastructure
  │                        │                        │                        │
  │  POST /ask             │                        │                        │
  │  {question, session}   │                        │                        │
  │───────────────────────▶│                        │                        │
  │                        │  AskUseCase.execute()  │                        │
  │                        │───────────────────────▶│                        │
  │                        │                        │  IVectorStore.search() │
  │                        │                        │───────────────────────▶│ pgvector + FTS
  │                        │                        │◀───────────────────────│ ranked chunks
  │                        │                        │                        │
  │                        │                        │  ILLMProvider.stream() │
  │                        │                        │───────────────────────▶│ Groq / Ollama
  │                        │                        │◀─ ─ ─ token stream ─ ─│
  │◀─ ─ ─ SSE tokens ─ ─ ─│◀─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │                        │
  │                        │                        │                        │
  │  SSE: answer + citations                        │                        │
  │◀───────────────────────│                        │                        │
```

- No job overhead — direct request/response with optional SSE token streaming.
- Correlation ID generated per request and propagated through all layers.
- Token/cost accounting persisted on completion.

### A.4.2 Async: Document Ingestion (T7 Job)

```
Client                    API              Celery/Redis           Worker            PostgreSQL
  │                        │                    │                   │                   │
  │  POST /documents       │                    │                   │                   │
  │  {file, metadata}      │                    │                   │                   │
  │───────────────────────▶│                    │                   │                   │
  │                        │  Create job record │                   │                   │
  │                        │──────────────────────────────────────────────────────────▶│
  │                        │  Enqueue task      │                   │                   │
  │                        │───────────────────▶│                   │                   │
  │  202 {job_id}          │                    │                   │                   │
  │◀───────────────────────│                    │                   │                   │
  │                        │                    │  Dequeue           │                   │
  │  GET /jobs/{id}/events │                    │──────────────────▶│                   │
  │───────────────────────▶│                    │                   │                   │
  │                        │                    │                   │  Extract          │
  │◀─ SSE: job_started ───│◀─ ─ Redis pub/sub ─│◀─ ─ progress ─ ─│                   │
  │                        │                    │                   │  Clean            │
  │◀─ SSE: step_completed─│◀─ ─ ─ ─ ─ ─ ─ ─ ─│◀─ ─ ─ ─ ─ ─ ─ ─│                   │
  │                        │                    │                   │  Chunk            │
  │◀─ SSE: step_completed─│                    │                   │  Embed            │
  │                        │                    │                   │  Index             │
  │                        │                    │                   │──────────────────▶│
  │◀─ SSE: job_completed──│◀─ ─ ─ ─ ─ ─ ─ ─ ─│◀─ ─ ─ ─ ─ ─ ─ ─│  checkpoint+done   │
  │                        │                    │                   │                   │
```

- API returns `202 Accepted` + `job_id` immediately.
- Worker processes pipeline stages, checkpointing after each stage.
- **Durable event architecture:** Every job event is **persisted to `job_events` in PostgreSQL** before or atomically with publication to Redis pub/sub. PostgreSQL `job_events` is the durable event history. Redis pub/sub is used only as a **low-latency notification channel**. SSE clients reconnecting after a disconnect **replay missed events from PostgreSQL** (using `sequence_number`) before subscribing to live Redis events. Redis loss therefore does not lose job history.
- Job state authoritative in PostgreSQL. SSE disconnect does not affect job.

### A.4.3 Async: Clinical Note Workflow (T7 Job + Approval Gate)

> **Key distinction:** The T7 **job lifecycle** and clinical **workflow state** are modelled separately. The job lifecycle remains `PENDING → QUEUED → STARTED → COMPLETED | FAILED | CANCELLED`, while the clinical workflow maintains its own state machine: `RESEARCH → SAFETY_CHECK → DRAFT → AWAITING_APPROVAL → FINALIZE`. This prevents domain workflow states from being conflated with infrastructure job lifecycle states.

```
Client          API          Celery       Worker                    PostgreSQL
  │              │              │            │                         │
  │ POST /wflow  │              │            │                         │
  │ {case_summary}              │            │                         │
  │─────────────▶│              │            │                         │
  │              │ create job   │            │                         │
  │              │ (PENDING)    │            │                         │
  │              │─────────────────────────────────────────────────────▶│
  │              │ enqueue      │            │                         │
  │              │─────────────▶│            │  job → QUEUED           │
  │ 202 {job_id} │              │            │                         │
  │◀─────────────│              │            │                         │
  │              │              │ dequeue    │  job → STARTED          │
  │              │              │───────────▶│                         │
  │              │              │            │                         │
  │              │              │            │ ── workflow: RESEARCH ──│
  │◀─ SSE: agent_started ──────│◀───────────│  AGT-01: search_corpus  │
  │◀─ SSE: tool_called ────────│◀───────────│  retrieve_drug_info     │
  │◀─ SSE: agent_completed ────│◀───────────│                         │
  │              │              │            │                         │
  │              │              │            │ ── workflow: SAFETY ────│
  │◀─ SSE: agent_started ──────│◀───────────│  AGT-02: check_interact │
  │◀─ SSE: tool_called ────────│◀───────────│  validate_dosage        │
  │◀─ SSE: agent_completed ────│◀───────────│                         │
  │              │              │            │                         │
  │              │              │            │ ── workflow: DRAFT ─────│
  │◀─ SSE: agent_started ──────│◀───────────│  AGT-03: draft_note     │
  │◀─ SSE: agent_completed ────│◀───────────│  (non-side-effecting)   │
  │              │              │            │                         │
  │              │              │            │ workflow → AWAITING_APPROVAL
  │              │              │            │ persist draft + approval │
  │◀─ SSE: awaiting_approval ──│◀───────────│ emit event ────────────▶│
  │              │              │            │ WORKER RELEASES SLOT    │
  │              │              │            │ (task returns)          │
  │              │              │            │                         │
  │ ─ ─ ─ ─ ─ ─ Reviewer inspects draft ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
  │              │              │            │                         │
  │ POST /approvals/{id}       │            │                         │
  │ {action: approve}          │            │                         │
  │─────────────▶│              │            │                         │
  │              │ ApproveUseCase:           │                         │
  │              │ ├─ persist decision ──────────────────────────────▶│
  │              │ ├─ audit action ──────────────────────────────────▶│
  │              │ └─ enqueue finalization ──▶│                        │
  │ 200 OK       │              │            │                         │
  │◀─────────────│              │ dequeue    │                         │
  │              │              │───────────▶│                         │
  │              │              │            │ workflow → FINALIZE     │
  │              │              │            │ finalize_clinical_note  │
  │              │              │            │ (WRITE — gated)────────▶│
  │              │              │            │ workflow → COMPLETED    │
  │              │              │            │ job → COMPLETED────────▶│
  │◀─ SSE: job_completed ──────│◀───────────│                         │
  │              │              │            │                         │
```

Key design decisions:
- The worker **does not remain blocked** while waiting for human approval. After producing the draft, it persists the workflow checkpoint and approval record, transitions the workflow to `AWAITING_APPROVAL`, emits the `awaiting_approval` event, and **releases the worker slot** (the Celery task returns).
- The approval command itself is **synchronous** (returns `200 OK` immediately). It persists the decision, records the audit entry, and **enqueues a finalization task** back to Celery.
- A new worker picks up the finalization task, resumes from the persisted checkpoint, executes `finalize_clinical_note` (the write/side-effecting tool), and completes the job.
- `finalize_clinical_note` is the **only** agent-accessible write/side-effecting tool and executes **only** after human approval.
- The **T7 job lifecycle and clinical workflow state remain independent.** When the worker reaches `AWAITING_APPROVAL`, the clinical workflow is paused at its persisted checkpoint and the current Celery task returns. The job remains in the `STARTED` lifecycle state while awaiting reviewer action because the overall workflow has not yet reached a terminal outcome. Once the reviewer approves, rejects, or the job is cancelled, a subsequent command/task completes the appropriate lifecycle transition. This allows worker capacity to be released without losing the logical job state.

---

## A.5 Component Architecture

### A.5.1 API Layer (Presentation)

| Component           | Responsibility                                                                         |
| ------------------- | -------------------------------------------------------------------------------------- |
| **Auth middleware**  | JWT validation, role extraction, request-scoped user context                           |
| **Rate limiter**    | Token-bucket per user, sliding window per IP (target: managed API gateway; MVP: in-process) |
| **Route handlers**  | Thin controllers delegating to application use cases via DI                            |
| **SSE endpoints**   | `GET /jobs/{id}/events` — on connect, replay missed events from PostgreSQL `job_events` (by `sequence_number`), then subscribe to Redis pub/sub for live events. Fan-out to connected clients. |
| **OpenAPI spec**    | Auto-generated by FastAPI; serves as the API documentation                             |
| **Error handler**   | Maps domain errors to HTTP status codes; never exposes internal details                |
| **CORS / Headers**  | Strict origin policy; security headers (X-Content-Type-Options, X-Frame-Options, etc.) |

### A.5.2 Orchestrator

| Aspect              | Design                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------- |
| **Pattern**         | **Pipeline** (sequential) — justified in ADR-002                                        |
| **Implementation**  | Application-layer state machine defining transitions; infrastructure adapter executes    |
| **Workflow states** | `RESEARCH → SAFETY_CHECK → DRAFT → AWAITING_APPROVAL → FINALIZE → COMPLETED` (domain workflow state, separate from T7 job lifecycle) |
| **T7 job lifecycle**| `PENDING → QUEUED → STARTED → COMPLETED | FAILED | CANCELLED` (infrastructure lifecycle, not conflated with workflow states) |
| **Controls**        | Max iterations (10), per-step timeout (60s), retry w/ backoff (3 retries), circuit breaker |
| **Degradation**     | Safety-aware: plain RAG for informational Q&A only. Clinical notes **never** bypass Safety Checker. |
| **Persistence**     | Every step persisted with: agent, tools called, chunks, I/O, tokens, cost, timestamps   |
| **Inspection**      | Full run trace queryable by run ID or correlation ID                                    |

### A.5.3 Agents

Each agent is defined by:
- An **explicit role** (system prompt from versioned `prompts/*.yaml`)
- A **restricted tool allow-list** (enforced at the application layer, not by the LLM)
- **Typed I/O contracts** (Pydantic models)
- A **termination condition** (max iterations, success criteria, or explicit failure)

| Agent                    | ID     | Tools                                    | Input Contract             | Output Contract              |
| ------------------------ | ------ | ---------------------------------------- | -------------------------- | ---------------------------- |
| Guideline Researcher     | AGT-01 | `search_corpus`, `retrieve_drug_info`    | `CaseSummary`              | `ResearchFindings`           |
| Safety Checker           | AGT-02 | `check_interactions`, `validate_dosage`  | `ResearchFindings`         | `SafetyReport`               |
| Documentation Drafter    | AGT-03 | `draft_clinical_note`                    | `SafetyReport`             | `ClinicalNoteDraft`          |

### A.5.4 Tools

| Tool                     | Type           | Agent        | Description                                                                  |
| ------------------------ | -------------- | ------------ | ---------------------------------------------------------------------------- |
| `search_corpus`          | Read           | AGT-01       | Hybrid retrieval (dense + FTS + RRF + reranker) against the document corpus  |
| `retrieve_drug_info`     | Read           | AGT-01       | Targeted retrieval for drug-specific information (formulary, interactions)    |
| `check_interactions`     | Read           | AGT-02       | Cross-references drugs/conditions against interaction database chunks        |
| `validate_dosage`        | Read           | AGT-02       | Verifies dosage claims against explicit corpus evidence; refuses if not found |
| `draft_clinical_note`    | Read (draft)   | AGT-03       | Produces structured clinical note draft with citations; non-side-effecting   |
| `finalize_clinical_note` | **Write/gated**| Orchestrator | Persists approved clinical note; **only executes after human approval**      |

**Tool argument validation:** All tool arguments are schema-validated via Pydantic before execution. The LLM's output is parsed against the tool's input schema — malformed or injection-bearing arguments are rejected with a typed error.

### A.5.5 RAG / Evidence Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        RAG Pipeline                                     │
│                                                                         │
│   User Query                                                            │
│       │                                                                 │
│       ▼                                                                 │
│   ┌──────────────┐                                                      │
│   │ Query        │  Optional: query rewriting, expansion                │
│   │ Processing   │                                                      │
│   └──────┬───────┘                                                      │
│          │                                                              │
│          ├─────────────────────┐                                         │
│          ▼                     ▼                                         │
│   ┌──────────────┐     ┌──────────────┐                                 │
│   │ Dense Search │     │ Sparse Search│                                 │
│   │ (pgvector    │     │ (PostgreSQL  │                                 │
│   │  cosine ANN) │     │  tsvector/   │                                 │
│   │              │     │  ts_rank)    │                                 │
│   └──────┬───────┘     └──────┬───────┘                                 │
│          │                     │                                         │
│          ▼                     ▼                                         │
│   ┌──────────────────────────────────┐                                  │
│   │  Reciprocal Rank Fusion (RRF)    │  k=60 (documented, tunable)     │
│   │  score = Σ 1/(k + rank_i)        │                                  │
│   └──────────────┬───────────────────┘                                  │
│                  │                                                       │
│                  ▼                                                       │
│   ┌──────────────────────────────────┐                                  │
│   │  Cross-Encoder Re-ranking        │  Enhancement (ADR-001)          │
│   │  (BAAI/bge-reranker-v2-m3)       │  Local, free, no API needed    │
│   └──────────────┬───────────────────┘                                  │
│                  │                                                       │
│                  ▼                                                       │
│   ┌──────────────────────────────────┐                                  │
│   │  Confidence Threshold Filter     │  Below threshold → REFUSE       │
│   │  (reranker score < threshold)    │  "Not enough information"       │
│   └──────────────┬───────────────────┘                                  │
│                  │                                                       │
│                  ▼                                                       │
│   Top-k chunks with structured citations:                               │
│   {document_id, name, section, page, chunk_id, reranker_score, snippet} │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Chunking strategy (ADR-001):**
- Structure-aware: split by section headings/boundaries first.
- Token-bounded: max 512 tokens per chunk with 64-token overlap.
- Metadata preserved: source document, section title, page number, heading hierarchy.
- Justified against clinical document structure where sections (dosage, interactions, contraindications) are natural semantic boundaries.

**Refusal mechanism:**
- If no chunk scores above the confidence threshold after re-ranking → refuse.
- If query targets dosage/contraindication and retrieved chunks don't contain explicit values → refuse (BR-01, BR-02).
- Refusal is a **correct system behaviour**, not an error.

### A.5.6 LLM Provider Architecture

```
Application Layer (Ports)                    Infrastructure Layer (Adapters)
┌─────────────────────────┐                 ┌─────────────────────────┐
│                         │                 │                         │
│  ┌───────────────────┐  │                 │  ┌───────────────────┐  │
│  │  ILLMProvider     │  │ ◀── implements ─│  │  GroqAdapter      │  │
│  │                   │  │                 │  │  (OpenAI-compat)  │  │
│  │  + complete()     │  │                 │  └───────────────────┘  │
│  │  + stream()       │  │                 │  ┌───────────────────┐  │
│  │  + tool_call()    │  │ ◀── implements ─│  │  OllamaAdapter    │  │
│  │                   │  │                 │  │  (local)          │  │
│  └───────────────────┘  │                 │  └───────────────────┘  │
│                         │                 │  ┌───────────────────┐  │
│  ┌───────────────────┐  │                 │  │  GeminiAdapter    │  │
│  │  IEmbeddingProvider│ │ ◀── implements ─│  │  (optional)       │  │
│  │                   │  │                 │  └───────────────────┘  │
│  │  + embed()        │  │                 │                         │
│  │  + embed_batch()  │  │                 │  ┌───────────────────┐  │
│  │                   │  │ ◀── implements ─│  │  SentenceTransf.  │  │
│  └───────────────────┘  │                 │  │  Adapter (local)  │  │
│                         │                 │  └───────────────────┘  │
│  ┌───────────────────┐  │                 │                         │
│  │  FallbackChain    │  │                 │  Configured per         │
│  │  (per capability) │  │                 │  capability:            │
│  │                   │  │                 │  chat: Groq→Ollama      │
│  │  + with_fallback()│  │                 │  embed: local→[optional]│
│  └───────────────────┘  │                 │                         │
└─────────────────────────┘                 └─────────────────────────┘
```

**Fallback chain behaviour:**
1. Try primary provider.
2. On failure (rate limit, timeout, connection error), log and try next.
3. Continue down the chain until success or all providers exhausted.
4. On full exhaustion: return a typed `AllProvidersExhaustedError`.
5. Fallback chain is **per capability** — chat and embedding chains are independent.
6. A provider is only included in a fallback chain when its adapter and required credentials/model are **configured**. The system does not require Gemini or any specific provider to operate. Minimum viable: Groq (chat) + sentence-transformers (embed).

### A.5.7 T7 Durable Job Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    T7 Job Lifecycle Architecture                         │
│                                                                          │
│  API                     Redis                    Workers                │
│  ┌───────────┐          ┌───────────┐           ┌───────────────┐       │
│  │ Submit    │──enqueue─▶│ Celery    │──dequeue──▶│ Process       │       │
│  │ (job_id)  │          │ Broker    │           │ (with         │       │
│  └───────────┘          └───────────┘           │  checkpoints) │       │
│       │                      │                   └───────┬───────┘       │
│       │                      │                           │               │
│       ▼                      ▼                           ▼               │
│  PostgreSQL (authoritative source of truth)                              │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  jobs table                                                      │    │
│  │  ─────────                                                       │    │
│  │  id (UUID)           │ status (PENDING/QUEUED/STARTED/...)       │    │
│  │  operation_type      │ attempt_number / max_attempts             │    │
│  │  idempotency_key     │ last_error / next_retry_at                │    │
│  │  user_id             │ created_at / started_at / completed_at    │    │
│  │  input_payload       │ checkpoint_data (JSONB)                   │    │
│  │  result_payload      │ worker_id / worker_heartbeat              │    │
│  │  correlation_id      │ cancellation_requested (bool)             │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Recovery Process (on worker startup / periodic):                        │
│  ────────────────                                                        │
│  SELECT * FROM jobs                                                      │
│  WHERE status = 'STARTED'                                                │
│    AND worker_heartbeat < NOW() - INTERVAL '5 minutes'                   │
│  → Re-queue these jobs, they resume from checkpoint_data                 │
│                                                                          │
│  Cancellation:                                                           │
│  ─────────────                                                           │
│  POST /jobs/{id}/cancel                                                  │
│  → SET cancellation_requested = TRUE in PostgreSQL + Redis               │
│  → Worker checks flag between steps → graceful termination               │
│                                                                          │
│  Idempotency:                                                            │
│  ────────────                                                            │
│  idempotency_key = SHA-256(operation_type + canonical_input + params)     │
│  → UNIQUE constraint prevents duplicate jobs                             │
│  → Re-submit returns existing job_id                                     │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**T7 Job lifecycle states (infrastructure):**

```
PENDING ──▶ QUEUED ──▶ STARTED ──┬──▶ COMPLETED
                                  ├──▶ FAILED ──(retry)──▶ QUEUED
                                  └──▶ CANCELLED
```

- `PROGRESS` is **not** a lifecycle state — a job remains `STARTED` while emitting progress events.
- `FAILED` + retry transitions back to `QUEUED` with incremented `attempt_number`.
- `attempt_number`, `max_attempts`, `last_error`, `next_retry_at` tracked per job.
- Automatic retries use exponential backoff; manual retries always permitted.

**Clinical workflow states (domain, inside a STARTED job):**

```
RESEARCH ──▶ SAFETY_CHECK ──▶ DRAFT ──▶ AWAITING_APPROVAL ──┬──▶ FINALIZE ──▶ COMPLETED
                                                              └──▶ REJECTED
```

- Workflow states are **domain concepts** tracked in the `runs` table.
- Job lifecycle states are **infrastructure concepts** tracked in the `jobs` table.
- A job can be `STARTED` while the workflow is `AWAITING_APPROVAL` — these are independent state machines.
- On approval, the finalization step is enqueued as a new Celery task that resumes the workflow from the persisted checkpoint.

### A.5.8 Human Approval Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Human Approval Architecture                           │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │  approvals table                                                │     │
│  │  ───────────────                                                │     │
│  │  id (UUID)           │ run_id (FK → runs)                       │     │
│  │  draft_content       │ status (PENDING/APPROVED/REJECTED)       │     │
│  │  final_content       │ reviewer_id (FK → users, role=reviewer)  │     │
│  │  reviewer_comment    │ action_at (timestamp)                    │     │
│  │  edit_diff           │ created_at                               │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  Workflow:                                                               │
│  ─────────                                                               │
│  1. Drafter produces draft → approval record created (PENDING)           │
│  2. Workflow transitions to AWAITING_APPROVAL (persisted)                │
│     Worker releases slot (Celery task returns)                           │
│  3. Reviewer sees draft in approval queue                                │
│  4. Reviewer actions (synchronous API command):                          │
│     ├── APPROVE  → final_content = draft_content                        │
│     │              persist decision + audit                             │
│     │              enqueue finalization task to Celery                   │
│     │              → worker resumes → finalize_clinical_note executes   │
│     │              → workflow → COMPLETED, job → COMPLETED              │
│     ├── EDIT+APPROVE → final_content = edited_content                   │
│     │                  edit_diff stored for audit                        │
│     │                  enqueue finalization task to Celery               │
│     │                  → same as APPROVE                                │
│     └── REJECT   → reviewer_comment stored                              │
│                    workflow → REJECTED                                  │
│                    job → COMPLETED                                      │
│                    (A rejected clinical note is a terminal workflow     │
│                    outcome, while the underlying T7 job completes       │
│                    successfully from an infrastructure perspective      │
│                    because the requested workflow was processed and     │
│                    a valid reviewer decision was recorded.)             │
│  5. All actions audited: who, when, what action, what changed           │
│                                                                          │
│  Security:                                                               │
│  ─────────                                                               │
│  - Only role=reviewer or role=admin can approve/reject/edit              │
│  - Server-side enforcement (not UI hiding)                               │
│  - finalize_clinical_note NEVER executes without approval record        │
│  - Audit trail is immutable (append-only)                                │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## A.6 Security & Trust Boundaries

### A.6.1 Trust Boundary Diagram

```
┌─────────────────────────────── TRUST BOUNDARY: SYSTEM ──────────────────────────────┐
│                                                                                      │
│  ┌────────────────── TRUST BOUNDARY: AUTHENTICATED ─────────────────────────────┐   │
│  │                                                                               │   │
│  │  ┌──────────────── TRUST BOUNDARY: AUTHORIZED (RBAC) ──────────────────┐    │   │
│  │  │                                                                      │    │   │
│  │  │  API Layer                Application Layer          Domain Layer    │    │   │
│  │  │  (auth validated)         (business logic)           (pure rules)   │    │   │
│  │  │                                                                      │    │   │
│  │  └──────────────────────────────────────────────────────────────────────┘    │   │
│  │                                                                               │   │
│  │  ┌──────────────── TRUST BOUNDARY: UNTRUSTED CONTENT ──────────────────┐    │   │
│  │  │                                                                      │    │   │
│  │  │  Retrieved document chunks ← ALWAYS UNTRUSTED DATA                  │    │   │
│  │  │  - Cannot alter system instructions                                  │    │   │
│  │  │  - Cannot alter tool permissions                                     │    │   │
│  │  │  - Cannot alter workflow state                                       │    │   │
│  │  │  - Sanitisation does NOT establish trust                             │    │   │
│  │  │                                                                      │    │   │
│  │  └──────────────────────────────────────────────────────────────────────┘    │   │
│  │                                                                               │   │
│  └───────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                      │
│  ┌──────────────── TRUST BOUNDARY: EXTERNAL (DATA EGRESS) ────────────────────┐    │
│  │                                                                              │    │
│  │  LLM Provider APIs (Groq, Gemini)                                           │    │
│  │  - Prompts sent with retrieved chunks (data leaves infrastructure)          │    │
│  │  - PII detection/redaction applied before sending                            │    │
│  │  - No PHI in corpus (design constraint)                                     │    │
│  │  - Documented in SECURITY.md: what data leaves, to whom, why                │    │
│  │                                                                              │    │
│  └──────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                      │
│  ┌──────────────── TRUST BOUNDARY: LOCAL (NO DATA EGRESS) ────────────────────┐    │
│  │                                                                              │    │
│  │  Ollama (local) / sentence-transformers (local)                             │    │
│  │  - All data stays on-premises                                               │    │
│  │  - Preferred for sensitive workloads                                         │    │
│  │                                                                              │    │
│  └──────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### A.6.2 Prompt Structure (Instruction/Content Separation)

```
┌─────────────────────────────────────────────────────────┐
│  SYSTEM PROMPT (authoritative — from versioned YAML)    │
│  ─────────────────────────────────────────────────────── │
│  You are a clinical guideline researcher.                │
│  You MUST only use information from the provided         │
│  evidence sections. If the evidence does not contain     │
│  the answer, respond: "Not enough information in the     │
│  corpus."                                                │
│  NEVER infer dosages or contraindications.               │
│  ...                                                     │
├─────────────────────────────────────────────────────────┤
│  EVIDENCE (untrusted — retrieved chunks, clearly marked)│
│  ─────────────────────────────────────────────────────── │
│  <evidence>                                              │
│  [Source: WHO-Guidelines-Hypertension.pdf, p.12, §3.2]  │
│  "First-line treatment for Stage 1 hypertension..."     │
│  </evidence>                                             │
│                                                          │
│  <evidence>                                              │
│  [Source: FDA-Lisinopril-Label.pdf, p.3, §Dosage]       │
│  "Initial dose: 10 mg once daily..."                    │
│  </evidence>                                             │
├─────────────────────────────────────────────────────────┤
│  USER QUERY (untrusted — user input)                    │
│  ─────────────────────────────────────────────────────── │
│  What is the recommended first-line treatment for        │
│  Stage 1 hypertension and what is the starting dosage?  │
└─────────────────────────────────────────────────────────┘
```

- System instructions are **authoritative** — they define behaviour.
- Evidence sections are **data** — the model must treat them as evidence to cite, not instructions to follow.
- The `<evidence>` wrapper creates a clear structural boundary.
- Any instruction-like content inside `<evidence>` tags is treated as data.

---

## A.7 Observability Architecture

### A.7.1 Target Observability Stack

| Component              | Target (Production)                            | Purpose                                          |
| ---------------------- | ---------------------------------------------- | ------------------------------------------------ |
| **Metrics**            | Prometheus + Grafana                           | System metrics, request rates, latencies         |
| **Tracing**            | OpenTelemetry → Tempo / Jaeger                 | Distributed traces across services               |
| **LLM Tracing**        | Custom trace store in PostgreSQL (or Langfuse) | Agent steps, tool calls, token/cost per LLM call |
| **Logging**            | Structured JSON → ELK / CloudWatch             | Application logs with correlation IDs            |
| **Alerting**           | Grafana Alerting / PagerDuty                   | SLA breaches, error rate spikes                  |
| **Cost Dashboard**     | Custom (PostgreSQL cost ledger + Grafana)       | Token spend per user/model/time period           |

### A.7.2 Correlation ID Flow

```
Request arrives → generate correlation_id (UUID)
    │
    ├─▶ API layer: attach to request context, log headers
    │
    ├─▶ Application layer: pass to use case, attach to run record
    │
    ├─▶ Orchestrator: attach to each agent step
    │
    ├─▶ Agent: attach to each tool call
    │
    ├─▶ LLM adapter: attach to each LLM API call, log tokens
    │
    └─▶ Infrastructure: attach to DB queries, Celery tasks
```

Every log line, trace span, and database record includes the `correlation_id`, enabling full reconstruction of any request's lifecycle.

### A.7.3 Token & Cost Accounting

```
┌──────────────────────────────────────────┐
│  cost_ledger table                        │
│  ────────────────                         │
│  id                 │ correlation_id      │
│  run_id             │ agent_id            │
│  model_name         │ provider            │
│  prompt_tokens      │ completion_tokens   │
│  total_tokens       │ estimated_cost_usd  │
│  created_at         │ user_id             │
│                                           │
│  Queryable by: user, model, time range,  │
│  run_id, correlation_id                   │
└──────────────────────────────────────────┘
```

---

## A.8 Data Architecture

### A.8.1 Entity-Relationship Overview

```
users ──────────< sessions
  │                  │
  │                  └───────< messages
  │
  ├──────────< documents ──────< chunks
  │                                │
  │                                └───< chunk_embeddings (pgvector)
  │
  ├──────────< jobs
  │              │
  │              ├───────< job_events (durable, ordered by sequence_number)
  │              │
  │              └───────< runs ──────────< run_steps
  │                          │                  │
  │                          │                  └───< step_tool_calls
  │                          │
  │                          └───────< approvals
  │
  └──────────< cost_ledger_entries

traces ──────< spans
```

### A.8.2 Key Tables

| Table                | Purpose                                                     | Key Columns                                                    |
| -------------------- | ----------------------------------------------------------- | -------------------------------------------------------------- |
| `users`              | Authentication & RBAC                                       | id, email, hashed_password, role (analyst/reviewer/admin)      |
| `documents`          | Ingested document metadata                                  | id, filename, content_hash, status, version, metadata          |
| `chunks`             | Document chunks with metadata                               | id, document_id, content, section, page, token_count           |
| `chunk_embeddings`   | Vector embeddings (pgvector)                                | chunk_id, embedding (vector), model_name                       |
| `jobs`               | T7 durable job records (authoritative)                      | id, type, status, idempotency_key, attempt_number, checkpoint  |
| `job_events`         | Durable ordered event history for job replay/reconnect      | id, job_id, sequence_number, event_type, payload (JSONB), correlation_id, created_at. **UNIQUE(job_id, sequence_number)** for deterministic ordering and reconnect/replay. |
| `runs`               | Workflow execution records (domain state)                   | id, job_id, user_id, correlation_id, workflow_status, timestamps |
| `run_steps`          | Per-step agent execution records                            | id, run_id, agent_id, step_order, input, output, tokens, cost  |
| `approvals`          | Human approval records                                      | id, run_id, draft, final, status, reviewer_id, action_at       |
| `sessions`           | Persistent conversation history                             | id, user_id, title, created_at                                 |
| `cost_ledger`        | Token/cost accounting per LLM call                          | id, run_id, model, prompt_tokens, completion_tokens, cost      |
| `traces`             | LLM tracing root records                                    | id, correlation_id, run_id, total_duration, total_tokens       |
| `spans`              | Individual trace spans (agent, tool, LLM)                   | id, trace_id, parent_span_id, operation, duration, metadata    |

---

## A.9 Scalability, Reliability & Recovery

### A.9.1 Target Scalability

| Dimension              | Target Design                                                                          |
| ---------------------- | -------------------------------------------------------------------------------------- |
| **API horizontal**     | Stateless FastAPI behind a load balancer; N instances                                   |
| **Worker horizontal**  | Celery workers scaled independently per queue (ingestion, workflow, evaluation)         |
| **Database**           | Managed PostgreSQL with read replicas for query load; connection pooling (PgBouncer)   |
| **Vector search**      | pgvector with IVFFlat/HNSW indexes (default). For >1M chunks, swap to dedicated vector DB (Qdrant or equivalent) via the `IVectorStore` port — `PostgresVectorAdapter` → `QdrantAdapter` — one adapter change, zero domain/application changes. |
| **Redis**              | Managed Redis (ElastiCache) for broker reliability; Redis Sentinel/Cluster for HA      |
| **Document storage**   | Object storage (S3) for original files; PostgreSQL for chunks and metadata             |
| **Embedding compute**  | GPU-enabled workers for local embedding/re-ranking at scale                            |

### A.9.2 Reliability

| Concern                | Design                                                                                  |
| ---------------------- | --------------------------------------------------------------------------------------- |
| **Job durability**     | PostgreSQL authoritative; `acks_late` + heartbeat recovery; checkpointing               |
| **Provider resilience**| Per-capability fallback chains; circuit breaker pattern on repeated failures             |
| **Data integrity**     | Foreign keys, unique constraints, ACID transactions; idempotent operations              |
| **Zero-downtime deploy** | Rolling deploys behind LB; DB migrations backward-compatible                          |

### A.9.3 Backup & Disaster Recovery

| Component          | Target                                                                                   |
| ------------------ | ---------------------------------------------------------------------------------------- |
| **PostgreSQL**     | Automated daily backups + WAL archiving for point-in-time recovery                       |
| **Redis**          | Ephemeral — no backup needed (PostgreSQL is authoritative)                               |
| **Documents**      | Object storage (S3) with versioning + cross-region replication                           |
| **RTO / RPO**      | RTO: 1 hour; RPO: 5 minutes (WAL-based)                                                 |

---

## A.10 Deployment Architecture

### A.10.1 Target Deployment

```
┌─────────────────────────────────────────────────────────────────┐
│                    Production Environment                        │
│                                                                  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ API (x3)   │  │ Workers  │  │ Managed  │  │ Managed      │  │
│  │ (ECS/K8s)  │  │ (x3-10)  │  │ Postgres │  │ Redis        │  │
│  │ autoscale  │  │ autoscale│  │ (RDS)    │  │ (ElastiCache)│  │
│  └────────────┘  └──────────┘  └──────────┘  └──────────────┘  │
│                                                                  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ API Gateway│  │ Secrets  │  │ S3       │  │ CloudWatch / │  │
│  │ (Kong/AWS) │  │ Manager  │  │ (docs)   │  │ Grafana      │  │
│  └────────────┘  └──────────┘  └──────────┘  └──────────────┘  │
│                                                                  │
│  CI/CD: GitHub Actions → staging → production (IaC: Terraform)  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### A.10.2 Cost Model at Scale

| Component              | Estimated Monthly Cost (moderate usage)                                    |
| ---------------------- | -------------------------------------------------------------------------- |
| Managed PostgreSQL     | $50–200 (depending on instance size, storage)                              |
| Managed Redis          | $15–50                                                                     |
| API compute (3 instances) | $30–100 (container-based, auto-scaled)                                  |
| Worker compute (3–10)  | $50–200 (scales with job volume)                                           |
| LLM API costs          | Usage-dependent; governed through model selection, token budgets, per-user/tenant cost controls, caching where applicable, and provider fallback chains |
| Object storage (S3)    | $1–5 (clinical corpus is relatively small)                                |
| Observability stack    | $20–50 (Grafana Cloud free tier or self-hosted)                           |
| **Total estimate**     | **$166–655/month** (moderate clinical team usage)                         |

---

# Part B — Implemented Architecture (Assessment MVP)

## B.1 What We Actually Build

The assessment implementation is a **fully functional MVP** running locally via Docker Compose. It demonstrates every architectural concept from Part A, with deliberate simplifications documented below.

### B.1.1 Implemented Components

| Component              | Implementation                                                                        |
| ---------------------- | ------------------------------------------------------------------------------------- |
| **API**                | Single FastAPI instance (Uvicorn)                                                     |
| **Workers**            | Single Celery worker process (multiple concurrent tasks)                              |
| **Database**           | PostgreSQL 16 + pgvector extension (Docker container)                                 |
| **Cache / Broker**     | Redis 7 (Docker container) — Celery broker + ephemeral state                          |
| **LLM (chat)**         | Groq (OpenAI-compatible) + Ollama (local fallback)                                    |
| **LLM (embeddings)**   | sentence-transformers (BGE-small-en, local) — no API needed                           |
| **Re-ranking**         | Cross-encoder (bge-reranker-v2-m3, local) — no API needed                             |
| **Auth**               | JWT + bcrypt, 3 roles (analyst/reviewer/admin)                                        |
| **Streaming**          | SSE via sse-starlette                                                                 |
| **Observability**      | Custom persisted trace store in PostgreSQL + correlation IDs                           |
| **PII**                | Presidio analyzer/anonymizer                                                          |
| **Frontend**           | Minimal web UI (plain functional — no design time spent)                              |
| **Packaging**          | `docker compose up` — single command, complete system                                 |
| **CI**                 | GitHub Actions: ruff, mypy, pytest, pip-audit, gitleaks                               |

### B.1.2 Technology Choices & Justification

| Decision                       | Choice                          | Why                                                                                     | Alternatives Considered                    |
| ------------------------------ | ------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------ |
| Web framework                  | FastAPI                         | Async-native, auto-OpenAPI, `Depends()` for DI, first-class Pydantic                   | Flask (sync), Django (too heavy)           |
| Relational + vector store      | PostgreSQL + pgvector           | One database for both; Alembic migrates both; simpler ops                               | Qdrant (separate service, stronger hybrid) |
| Keyword retrieval              | PostgreSQL FTS (tsvector)       | No extra service; good enough for clinical docs; keeps compose lean                     | Elasticsearch (overkill), rank_bm25 (in-memory) |
| Job queue                      | Celery + Redis                  | Battle-tested, restart survival, Python-native, `acks_late`                             | Arq (lighter, less mature), RQ (simpler)   |
| LLM orchestration              | Custom pipeline state machine   | Maximally teachable, zero framework coupling, Clean Architecture holds                  | LangGraph (framework lock-in risk)         |
| Embeddings                     | sentence-transformers (local)   | Free, no API dependency, fast enough for corpus size                                    | OpenAI embeddings (paid), Jina (API)       |
| Auth                           | JWT + pyjwt + passlib[bcrypt]   | Stateless, well-understood, lightweight                                                 | OAuth2 (complexity), session-based (stateful) |
| Observability                  | Custom PostgreSQL trace store   | No extra service; queryable; satisfies requirements                                     | Langfuse (richer UI, extra Docker service) |

### B.1.3 Deliberate Simplifications

| Simplification                                | Production Equivalent                  | Rationale                                              |
| --------------------------------------------- | -------------------------------------- | ------------------------------------------------------ |
| Single API process                            | Multiple instances behind LB           | Assessment runs locally; no real traffic                |
| Single Celery worker                          | Multiple specialised workers           | Sufficient for demo workload                           |
| PostgreSQL in Docker (no backups)             | Managed PostgreSQL with automated DR   | Assessment scope; data is synthetic                    |
| Redis in Docker (no persistence)              | Managed Redis with persistence         | Redis is ephemeral by design (AR-6)                    |
| In-process rate limiter                       | API gateway rate limiting              | Doesn't survive horizontal scaling; documented         |
| No API gateway                                | Kong / AWS API Gateway                 | Single instance doesn't need routing/gateway           |
| No secrets manager                            | HashiCorp Vault / AWS Secrets Manager  | `.env` file is acceptable for assessment               |
| No object storage                             | S3 for document originals              | Documents stored in PostgreSQL (small corpus)          |
| No centralized logging                        | ELK / CloudWatch                       | Structured JSON logs to stdout; sufficient             |

---

## B.2 Target → Implemented Gap Table

> **This table is the core of Part B.** Every gap is deliberate, documented, and includes the interim mitigation and effort to close.

| Target Component                         | Implemented? | Why Deferred                                                                      | Interim Mitigation                                                             | Effort & Cost to Close                    |
| ---------------------------------------- | ------------ | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------- |
| **API Gateway** (managed rate limiting, routing, auth termination) | ❌ No | Requires infrastructure beyond Docker Compose; unnecessary for single-instance    | In-process token-bucket rate limiter in FastAPI middleware; auth in app layer   | ~4h + $15–30/month (managed gateway)     |
| **Secrets Manager** (Vault, AWS SM)      | ❌ No        | No production deployment; `.env` is standard for assessment                       | `.env.example` with no real secrets; `.gitignore` excludes `.env`; gitleaks CI  | ~2h + $0–10/month                        |
| **Message Broker** (managed Redis/RabbitMQ) | ❌ No     | Docker Redis sufficient for assessment; ephemeral by design                       | Redis in Docker Compose; PostgreSQL is authoritative for all durable state     | ~1h + $15–50/month (ElastiCache)         |
| **Managed PostgreSQL** (RDS, Cloud SQL)  | ❌ No        | Local Docker sufficient; no real clinical data to protect                         | PostgreSQL in Docker Compose with volume; Alembic migrations                   | ~2h + $50–200/month (RDS)                |
| **Horizontal API scaling**               | ❌ No        | Single user assessment; no real traffic                                            | Single Uvicorn process with async handlers                                     | ~3h + load balancer cost                  |
| **Horizontal worker scaling**            | ❌ No        | Demo workload; single worker handles job queue                                     | Single Celery worker with concurrency; job checkpointing works at any scale    | ~2h + compute cost                        |
| **Object storage** (S3 for documents)    | ❌ No        | Small corpus (≤30 docs); PostgreSQL storage is sufficient                         | Documents stored as rows in PostgreSQL; content_hash for dedup                 | ~3h + $1–5/month                          |
| **Centralized observability** (Grafana+Tempo+Prometheus) | ❌ No | Extra Docker services; assessment observability is via custom trace store  | Custom PostgreSQL trace store with correlation IDs; queryable via API           | ~6h + $20–50/month (Grafana Cloud)        |
| **Automated backups / DR**               | ❌ No        | Synthetic data; no recovery requirements for assessment                            | Docker volume for PostgreSQL persistence; Alembic can recreate schema          | ~3h + $5–20/month (automated snapshots)  |
| **CDN / WAF / DDoS protection**          | ❌ No        | Local-only deployment; no public exposure                                          | None needed for assessment                                                      | ~2h + $20–100/month (CloudFlare/AWS)     |
| **CI/CD environments** (staging + prod)  | ❌ No        | Assessment has one environment; CI validates on every PR                            | GitHub Actions CI on every PR; manual `docker compose up`                       | ~4h + compute costs                       |
| **GPU-enabled embedding workers**        | ❌ No        | Small corpus; CPU inference sufficient for BGE-small                               | CPU-based sentence-transformers; embedding runs during ingestion, not at query time | ~2h + $50–200/month (GPU instances) |
| **Zero-downtime deployments**            | ❌ No        | Single instance; no production traffic                                              | Stop + `docker compose up` is acceptable                                        | ~3h (rolling deploy configuration)        |
| **Production HTTPS/TLS**                 | ❌ No        | Local-only; no public endpoint                                                     | HTTP for local development; documented as production requirement                | ~1h (Let's Encrypt + reverse proxy)       |
| **PgBouncer connection pooling**         | ❌ No        | Single API + single worker; connection pool overhead unnecessary                    | SQLAlchemy connection pool (built-in)                                           | ~1h + configuration                       |

---

## B.3 Significant Design Decisions

### Decision 1: Custom State Machine over LangGraph

**Chosen:** Custom pipeline state machine in the application layer.

**Rejected:** LangGraph.

**Rationale:** LangGraph would introduce a framework dependency into the application layer, violating C1 (Clean Architecture constraint). A custom state machine is ~200 lines of Python, maximally teachable, has zero framework coupling, and maps cleanly to our pipeline pattern (ADR-002). The trade-off is implementing retry/timeout/breaker logic ourselves rather than getting it from a framework — but this is straightforward and more defensible in the assessment.

### Decision 2: PostgreSQL FTS over Elasticsearch

**Chosen:** PostgreSQL full-text search (`tsvector` + `ts_rank`).

**Rejected:** Elasticsearch, OpenSearch.

**Rationale:** Adding a dedicated search engine means another Docker service, another failure mode, and another thing to explain. PostgreSQL FTS is adequate for a corpus of ≤30 clinical documents. The keyword component just needs to capture queries that dense embeddings miss (acronyms, drug codes, exact names). The gap: at >10,000 documents, dedicated search would outperform FTS. Documented in ADR-001.

### Decision 3: Custom Trace Store over Langfuse

**Chosen:** Custom PostgreSQL trace store.

**Rejected:** Langfuse (self-hosted).

**Rationale:** Langfuse requires its own Docker service (Langfuse + its own PostgreSQL). Our custom store keeps the Docker Compose simpler, satisfies the requirement equally ("self-hosted tool, OpenTelemetry, or a clean custom trace store — all equally acceptable"), and demonstrates understanding of what a trace store actually does. The gap: Langfuse has a better visualization UI. Mitigated by our API providing query endpoints for traces.

### Decision 4: Celery over Arq

**Chosen:** Celery + Redis.

**Rejected:** Arq, RQ.

**Rationale:** T7 requires restart survival, resumability, and cancellation. Celery's `acks_late` + `reject_on_worker_lost` provide the foundation (though PostgreSQL checkpoints are the real durability mechanism — per T7-04). Celery is battle-tested with extensive documentation, which matters for teachability. Arq is lighter but less mature. RQ is too simple for our requirements. Documented in ADR-004.

### Decision 5: Separate ILLMProvider and IEmbeddingProvider

**Chosen:** Two separate interfaces.

**Rejected:** Single unified interface.

**Rationale:** In practice, the best embedding model is often local (sentence-transformers) while the best chat model is hosted (Groq). A single interface would force every provider to implement both capabilities, even when it doesn't make sense. Per-capability fallback chains are also cleaner with separate interfaces. This is a direct application of the Interface Segregation Principle.

---

## B.4 Evidence Mapping to BRD Requirements

> This section will be populated as implementation progresses. Each BRD requirement ID will map to specific PRs, test files, and API endpoints that demonstrate compliance.

| BRD Requirement | Evidence Type            | Evidence Location             | Status     |
| --------------- | ------------------------ | ----------------------------- | ---------- |
| FR-1            | Code + integration tests | `app/`, `tests/integration/`  | ❌ Pending |
| FR-2            | Code + eval harness      | `app/`, `tests/`, evaluation/ | ❌ Pending |
| FR-3            | Eval results             | `docs/EVALUATION.md`          | ❌ Pending |
| FR-4            | Code + contract tests    | `app/`, `tests/unit/`         | ❌ Pending |
| FR-5            | Code + sequence diagram  | `app/`, `docs/ARCHITECTURE.md`| ❌ Pending |
| FR-6            | Code + demo video        | `app/`, README video link     | ❌ Pending |
| FR-7            | OpenAPI spec + UI        | `/docs`, frontend/            | ❌ Pending |
| FR-8            | Code + tests             | `app/`, `tests/unit/`         | ❌ Pending |
| FR-9            | Code + trace queries     | `app/`, API endpoints         | ❌ Pending |
| T7-*            | Code + restart test      | `app/`, `tests/integration/`  | ❌ Pending |
| BR-01–06        | Eval adversarial cases   | `docs/EVALUATION.md`          | ❌ Pending |
| SEC-*           | Security doc + tests     | `docs/SECURITY.md`, tests/    | ❌ Pending |

---

## B.5 Honest Assessment of Expedient Choices

> Candour here works in your favour.

1. **In-process rate limiter will not survive horizontal scaling.** We built a token-bucket middleware in FastAPI because deploying a managed API gateway for a single Docker Compose instance would be over-engineering the assessment. Closing the gap requires moving rate limiting to an API gateway (~4h, $15–30/month).

2. **No automated backup/recovery.** Synthetic data has no recovery requirements, but a production healthcare system absolutely needs automated PostgreSQL backups with point-in-time recovery. This is an operational gap, not an architectural one — the application code wouldn't change.

3. **Embedding model is deliberately small.** We use `bge-small-en` (33M params) for embeddings because it runs fast on CPU. A production system would use `bge-base-en` or `bge-large-en` on GPU for better retrieval quality. This is a tunable knob, not an architectural change.

4. **The approval gate does not implement SLA timers or escalation.** Those are T5 (Human Review Queue) requirements, not T7. Our approval gate covers the assessment requirements (approve/reject/edit, audited, server-enforced) but is not a full queue management system.

---

*This document is a study aid and architecture reference. Every decision should be defensible in an ADR. Every gap should be honest about what it costs to close. The system is designed to be taught — if you cannot explain a choice, reconsider it.*
