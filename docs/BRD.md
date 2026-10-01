# Business Requirements Document (BRD)

## Domain Copilot — D0 Healthcare + T7 Async Long-Running Jobs

| Field              | Value                                                        |
| ------------------ | ------------------------------------------------------------ |
| **Document ID**    | BRD-DC-D0T7                                                  |
| **Version**        | 1.2                                                          |
| **Author**         | *(Fady Ehab)*                                                |
| **Date**           | 2026-09-27                                                   |
| **Variant**        | D0 Healthcare · T7 Async Long-Running Jobs                   |
| **Derivation**     | National ID last two digits 91 mod 7 = 0 → D0; digit sum 31 mod 8 = 7 → T7 |

---

## 1. Context & Background

### 1.1 Problem Statement

Healthcare organisations maintain large bodies of specialised clinical documents — drug formularies, clinical practice guidelines (CPGs), interaction databases, and institutional protocols. Clinicians spend significant time manually searching, cross-referencing, and drafting documentation. This process is error-prone: missed contraindications, incorrect dosages, or unsupported clinical claims can directly harm patients.

### 1.2 Proposed Solution

**Domain Copilot** is an agentic RAG (Retrieval-Augmented Generation) platform that ingests the clinical document corpus, answers questions with verifiable citations, and executes a multi-step professional workflow through a team of specialised AI agents — while a clinician approves anything consequential.

The system follows three binding principles:

1. **Grounded, never guessing** — Every externally verifiable clinical or evidence claim must trace to a source chunk. System metadata and workflow-state statements (e.g., job status, document counts) are sourced from system state. "Not enough information in the corpus" is a correct and required answer.
2. **The human holds the pen** — The clinician must explicitly approve any drafted clinical note before it is finalised.
3. **Everything is observable** — Any agent run can be inspected: which agent ran, which tools it called, which chunks it retrieved, what it cost.

### 1.3 Assigned Variant

- **Domain Pack D0 — Healthcare:** Clinical evidence & documentation workflow.
- **Mandatory Twist T7 — Async Long-Running Jobs:** All significant operations (ingestion, agent workflows, evaluation) execute as background jobs on a real queue with workers. Submission returns immediately with a job ID. Progress is pushed to the client. Jobs survive server restart, are resumable, cancellable, and idempotent.

### 1.4 Execution Model (Sync vs Async)

Not all operations are async. The following table defines the execution model to prevent over-engineering simple operations:

| Operation                       | Execution Mode                  | Rationale                                                                 |
| ------------------------------- | ------------------------------- | ------------------------------------------------------------------------- |
| Simple Q&A / ask-with-citations | **Synchronous** + optional LLM SSE token streaming | Fast, stateless query; no job overhead needed                    |
| Document ingestion              | **Async T7 job**                | Long-running, multi-stage pipeline; must survive restarts                 |
| Clinical note workflow          | **Async T7 job**                | Multi-agent pipeline with approval gate; long-running and stateful        |
| Evaluation harness run          | **Async T7 job**                | Processes ≥25 Q/A pairs; potentially minutes to complete                  |
| Approval action (approve/reject/edit) | **Synchronous command**   | Immediate user action; updates job state and triggers finalisation        |
| Job cancellation                | **Synchronous command**         | Sets cancellation flag; worker checks cooperatively                       |
| Trace / history / job queries   | **Synchronous query**           | Read-only database queries; no background processing needed               |
| Health / readiness checks       | **Synchronous query**           | Lightweight system status checks                                          |

---

## 2. Personas

| ID     | Persona                | Role Description                                                                                                   | Key Needs                                                                                        |
| ------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| PER-01 | **Clinician (Analyst)**| A healthcare professional who queries the system for clinical guidance, reviews case summaries, and requests notes. | Grounded answers with citations; correct refusal on uncertain queries; real-time progress feedback |
| PER-02 | **Senior Clinician (Reviewer)** | An experienced clinician who reviews and approves/rejects/edits drafted clinical notes before finalisation. | Approval queue with priority; SLA visibility; edit-and-approve capability; audit trail            |
| PER-03 | **System Administrator** | Manages document ingestion, user accounts, system configuration, and monitors system health.                     | Bulk ingestion status; job management; observability dashboards; cost monitoring                  |

---

## 3. Objectives & Success Criteria

| ID     | Objective                                             | Measurable Success Criterion                                                                                     |
| ------ | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| OBJ-01 | Deliver grounded clinical answers                     | Retrieval hit-rate ≥ 70% on golden set; groundedness score ≥ 80%; zero unsupported/hallucinated dosage claims in the adversarial dosage evaluation cases |
| OBJ-02 | Enforce clinical safety through human approval        | 100% of drafted clinical notes require explicit clinician approval before finalisation; all approvals audited     |
| OBJ-03 | Correct refusal on insufficient evidence              | Refusal correctness ≥ 90% on adversarial cases (out-of-corpus, ambiguous, conflicting)                           |
| OBJ-04 | Full observability of every agent run                 | Every run inspectable by run ID: agents, tools, chunks, tokens, cost, timestamps                                 |
| OBJ-05 | Async-first architecture (T7)                         | All long-running operations return immediately with job ID; progress pushed via SSE; jobs survive worker restart  |
| OBJ-06 | Enterprise-grade architecture                         | Swapping LLM provider or vector store = config + one adapter; no LLM/vector SDK imports in domain/application    |
| OBJ-07 | Teachable system                                      | Teaching pack complete; both videos delivered; any line of code explainable and defensible                        |

---

## 4. Functional Requirements

### FR-1 — Document Ingestion

| Field                | Detail                                                                                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **ID**               | FR-1                                                                                                                                                   |
| **Title**            | Document Ingestion Pipeline                                                                                                                            |
| **Priority**         | P0 — Core                                                                                                                                              |
| **Description**      | The system shall ingest clinical documents in ≥2 formats (PDF, Markdown) through a separable, testable pipeline: extract → clean → chunk → embed → index. |
| **Acceptance Criteria** |                                                                                                                                                     |

- **AC-1.1:** System accepts at least PDF and Markdown document formats.
- **AC-1.2:** Pipeline stages (extract, clean, chunk, embed, index) are separable and independently testable.
- **AC-1.3:** Each document and chunk stores metadata: source filename, section/heading, page number, version, ingestion timestamp.
- **AC-1.4:** Re-ingesting the same document does not create duplicate chunks (idempotent).
- **AC-1.5:** Each document has a tracked ingestion status: `pending`, `processing`, `completed`, `failed`.
- **AC-1.6:** On failure, the specific stage and error message are recorded and retrievable.
- **AC-1.7:** (T7) Ingestion runs as an async background job; the API returns a job ID immediately. Progress events are pushed to the client.

---

### FR-2 — Retrieval & Citations

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-2                                                                                                          |
| **Title**            | Hybrid Retrieval with Citations and Refusal                                                                   |
| **Priority**         | P0 — Core                                                                                                     |
| **Description**      | The system shall retrieve relevant document chunks using hybrid retrieval (dense embeddings + keyword/FTS) with Reciprocal Rank Fusion, enhanced by cross-encoder re-ranking. Every answer must include structured citations traceable to the exact chunk. The system must correctly refuse when evidence is insufficient. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-2.1:** Hybrid retrieval combines dense (pgvector cosine similarity) and sparse (PostgreSQL full-text search with `tsvector`/`ts_rank`).
- **AC-2.2:** Results are fused using Reciprocal Rank Fusion (RRF) with a documented k parameter.
- **AC-2.3:** One retrieval enhancement is implemented: **cross-encoder re-ranking** (justified in ADR).
- **AC-2.4:** Chunking strategy is structure-aware (by section/heading) with token-bounded windows and overlap, documented and justified against the clinical document structure in an ADR.
- **AC-2.5:** Every answer includes structured citations: `{document_id, document_name, section, page, chunk_id, relevance_score, text_snippet}`. The `relevance_score` is the **reranker score** (cross-encoder) when re-ranking is applied, or the **RRF fused score** when re-ranking is not available.
- **AC-2.6:** On low-evidence queries (no chunks above the confidence threshold), the system responds with "Not enough information in the corpus" — never guesses.
- **AC-2.7:** (D0 Risk) The system must **refuse** rather than infer any dosage, contraindication, or drug interaction not explicitly stated in retrieved chunks.

---

### FR-3 — Evaluation Harness

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-3                                                                                                          |
| **Title**            | Evaluation Harness with Golden Set                                                                            |
| **Priority**         | P0 — **Never Cut**                                                                                            |
| **Description**      | A runnable evaluation harness with a golden set of ≥25 Q/A pairs, reporting retrieval hit-rate, groundedness, and refusal correctness. Includes ≥5 adversarial cases (prompt injection cases count toward this total). |
| **Acceptance Criteria** |                                                                                                            |

- **AC-3.1:** Golden set contains ≥25 question/answer pairs covering the healthcare corpus.
- **AC-3.2:** ≥5 adversarial cases (prompt injection cases **count toward** this total): out-of-corpus questions, ambiguous queries, prompt injection (direct), prompt injection (indirect via ingested document), conflicting sources.
- **AC-3.3:** Of the adversarial cases, ≥3 are prompt injection cases that the system demonstrably resists (including at least one indirect injection via poisoned document content — the headline threat for D0).
- **AC-3.4:** Harness is runnable via a single command (e.g., `python -m app.evaluation.run`).
- **AC-3.5:** Harness reports: retrieval hit-rate, groundedness score, refusal correctness rate.
- **AC-3.6:** Real baseline numbers are recorded — including bad ones — with written interpretation.
- **AC-3.7:** (D0-specific) Includes adversarial cases specifically targeting hallucinated dosage and contraindication responses.

---

### FR-4 — Multi-Agent System

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-4                                                                                                          |
| **Title**            | Specialised Healthcare Agents with Typed Contracts                                                            |
| **Priority**         | P0 — Core                                                                                                     |
| **Description**      | The system shall implement ≥3 specialised agents + an orchestrator, each with an explicit role, a restricted tool set, defined typed I/O, and a termination condition. ≥6 tools, of which ≥1 is write/side-effecting and never executes without passing the approval gate. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-4.1:** Three specialised agents are implemented:

  | Agent ID  | Agent Name               | Role                                                                                                         | Tools (Allow-List)                                           |
  | --------- | ------------------------ | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ |
  | AGT-01    | **Guideline Researcher** | Receives a case summary, retrieves relevant clinical guidelines, drug information, and protocol references.   | `search_corpus`, `retrieve_drug_info`                        |
  | AGT-02    | **Safety Checker**       | Reviews retrieved guidance for drug interactions, contraindications, dosage validity, and flags safety risks. | `check_interactions`, `validate_dosage`                      |
  | AGT-03    | **Documentation Drafter**| Synthesises verified findings into a structured clinical note with citations, ready for clinician review.     | `draft_clinical_note` (read — produces draft, **non-side-effecting**) |

- **AC-4.2:** An **Orchestrator** coordinates the agent pipeline: Researcher → Safety Checker → Drafter → Pending Approval → Finalise.
- **AC-4.3:** 6 tools in the baseline implementation: `search_corpus` (read), `retrieve_drug_info` (read), `check_interactions` (read), `validate_dosage` (read), `draft_clinical_note` (read — produces draft), `finalize_clinical_note` (**write/side-effecting — approval-gated**).
- **AC-4.4:** Each agent has a restricted tool allow-list — agents cannot call tools outside their set.
- **AC-4.5:** Agents communicate via **typed contracts** (Pydantic models), not free-form text.
- **AC-4.6:** Each agent has a defined termination condition (max iterations, success criteria, or explicit failure).
- **AC-4.7:** The `draft_clinical_note` tool produces a draft (non-side-effecting). The `finalize_clinical_note` tool (**write/side-effecting**) persists the approved note and **never executes** without passing the human approval gate.
- **AC-4.8:** The draft must exist and be visible to the reviewer before approval is possible. The reviewer approves/rejects/edits the *draft*; only on approval does `finalize_clinical_note` execute.

---

### FR-5 — Orchestration & Approval Gate

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-5                                                                                                          |
| **Title**            | Pipeline Orchestration with Human Approval Gate                                                               |
| **Priority**         | P0 — Core                                                                                                     |
| **Description**      | The orchestrator follows a **pipeline pattern** (justified in ADR). Mandatory controls: max-iteration breaker, per-step timeout, retry with backoff, graceful degradation (safety-aware). Every run is inspectable step-by-step by run ID. The approval gate supports approve / reject / edit-and-approve, all audited. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-5.1:** Orchestration follows a named, justified pattern: **Pipeline** (sequential: Research → Safety Check → Draft → Pending Approval → Finalise). Justified in ADR-002.
- **AC-5.2:** Max-iteration breaker prevents infinite loops (configurable, default: 10).
- **AC-5.3:** Per-step timeout prevents runaway agents (configurable, default: 60s per step).
- **AC-5.4:** Retry with exponential backoff on transient LLM failures (max 3 retries).
- **AC-5.5:** **Safety-aware degradation:** If the multi-agent pipeline fails, plain RAG fallback may answer **informational questions** (direct retrieval + single LLM call). However, plain RAG fallback **MUST NOT produce or finalise a clinical note**. Any clinical note workflow failure involving the Safety Checker results in a **safe failure/refusal** rather than bypassing the Safety Checker. This respects BR-03.
- **AC-5.6:** Every run is persisted and inspectable by run ID: sequence of steps, agent used, tools called, chunks retrieved, input/output, tokens consumed, cost, timestamps, status.
- **AC-5.7:** **Approval gate** — when the Drafter produces a clinical note draft:
  - The run transitions to `PENDING_APPROVAL` and persists the draft + pending state.
  - The draft is visible to the clinician reviewer for inspection.
  - The clinician reviewer can: **approve**, **reject** (with reason), or **edit-and-approve** (modify the draft, then approve).
  - All approval actions are **audited** (who, when, what action, what changed, diff if edited).
  - On **approval**: `finalize_clinical_note` (the write/side-effecting tool) executes, persisting the approved note. The run transitions to `COMPLETED`.
  - On **rejection**: the run is marked `REJECTED` with the reason. No note is finalised.
- **AC-5.8:** (T7) The entire orchestration pipeline runs as an async background job.

---

### FR-6 — Real-Time Streaming & Progress

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-6                                                                                                          |
| **Title**            | Token-Level Streaming and Live Progress Events                                                                |
| **Priority**         | P1 — Required                                                                                                 |
| **Description**      | The system shall provide token-level streaming via SSE, live agent progress events, and explicit client-initiated cancellation that actually stops server-side work. Two distinct SSE event categories are defined: job/workflow events and LLM streaming events. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-6.1:** Token-level streaming via Server-Sent Events (SSE) for LLM responses.
- **AC-6.2:** Two categories of SSE events are defined:

  **Job/Workflow events** (event type: `job`):
  - `job_queued`, `job_started`, `agent_started`, `agent_completed`, `tool_called`, `step_completed`, `awaiting_approval`, `job_completed`, `job_failed`, `job_cancelled`

  **LLM streaming events** (event type: `llm`):
  - `token` (individual token from LLM response), `stream_completed`

- **AC-6.3:** Client cancellation is performed via an **explicit `POST /jobs/{id}/cancel` endpoint** — not by closing the SSE connection. SSE is a read/stream channel; disconnecting does not cancel a clinical workflow. Cancellation is cooperative: the worker checks a cancellation flag (in Redis or database) between steps and terminates gracefully.
- **AC-6.4:** (T7) Progress events are pushed for background jobs via the SSE job event channel. Clients subscribe to `GET /jobs/{id}/events` (SSE) to receive real-time updates.
- **AC-6.5:** (T7) **SSE disconnection must not terminate or alter the underlying job.** The job remains durable in PostgreSQL regardless of whether the SSE connection is open. A client may reconnect to `GET /jobs/{id}/events` at any time and resume receiving events from the current state.

---

### FR-7 — API Surface & UI

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-7                                                                                                          |
| **Title**            | HTTP API (OpenAPI) and Minimal Working UI                                                                     |
| **Priority**         | P1 — Required (UI richness is first to cut)                                                                   |
| **Description**      | A documented HTTP API with auto-generated OpenAPI spec, plus a minimal working UI covering all core interactions. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-7.1:** HTTP API documented via FastAPI's auto-generated OpenAPI spec at `/docs`.
- **AC-7.2:** API covers: document ingestion, ask-with-citations, run-the-workflow, act-on-approval-gate (approve/reject/edit), view-a-trace, job management.
- **AC-7.3:** Minimal working UI (web) covering:
  - Ingest documents
  - Ask questions with citations displayed
  - Run the clinical note workflow
  - Act on the approval gate (approve / reject / edit-and-approve)
  - View a run trace
  - View job status and progress
- **AC-7.4:** Persistent session/conversation history.
- **AC-7.5:** (T7) UI shows job queue status, progress bars, and supports job cancellation.

---

### FR-8 — Authentication & Access Control

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-8                                                                                                          |
| **Title**            | Authentication with Role-Based Access Control                                                                 |
| **Priority**         | P0 — Core                                                                                                     |
| **Description**      | JWT-based authentication with ≥3 roles with genuinely different permissions, enforced server-side. Roles align with personas (PER-01, PER-02, PER-03). |
| **Acceptance Criteria** |                                                                                                            |

- **AC-8.1:** JWT-based authentication with secure password hashing (bcrypt/argon2).
- **AC-8.2:** Three roles with genuinely different permissions:

  | Role         | Persona  | Permissions                                                                                     |
  | ------------ | -------- | ----------------------------------------------------------------------------------------------- |
  | `analyst`    | PER-01   | Ask questions, run workflows, view own runs/jobs/traces, view own session history                |
  | `reviewer`   | PER-02   | All analyst permissions + approve/reject/edit clinical notes, view all pending approvals, view all runs |
  | `admin`      | PER-03   | All reviewer permissions + ingest documents, manage users, manage all jobs, view system health/observability, cost dashboards |

- **AC-8.3:** Permissions enforced **server-side** (not by hiding buttons) — API endpoints validate role on every request.
- **AC-8.4:** Object-ownership checks: an analyst cannot access another user's runs or jobs (unless reviewer/admin).

---

### FR-9 — Observability & Cost Accounting

| Field                | Detail                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| **ID**               | FR-9                                                                                                          |
| **Title**            | Correlation IDs, Token/Cost Accounting, Tracing, Health                                                       |
| **Priority**         | P0 — Core                                                                                                     |
| **Description**      | A correlation ID flows from request through orchestrator, agents, and LLM calls. Per-request token and cost accounting is persisted and queryable. LLM tracing via a custom persisted trace store. Health/readiness endpoints. |
| **Acceptance Criteria** |                                                                                                            |

- **AC-9.1:** A unique **correlation ID** (UUID) is generated per request and propagated: request → orchestrator → agent → LLM call → tool execution.
- **AC-9.2:** Per-request **token accounting**: prompt tokens, completion tokens, total tokens, per model.
- **AC-9.3:** Per-request **cost accounting**: estimated cost based on model pricing, persisted in a cost ledger table.
- **AC-9.4:** LLM **tracing** via a custom persisted trace store in PostgreSQL: each trace contains spans for every step (agent invocation, tool call, LLM call) with inputs, outputs, tokens, duration, and status.
- **AC-9.5:** Traces are queryable by run ID, correlation ID, user, and time range.
- **AC-9.6:** **Health endpoint** (`GET /health`) returns system status: API, database, Redis, worker connectivity.
- **AC-9.7:** **Readiness endpoint** (`GET /ready`) returns whether the system is ready to accept requests (dependencies connected, models loadable).

---

## 5. Architecture & Engineering Requirements

### AR-1 — Clean Architecture

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-1  | Clean Architecture (Hexagonal/Ports & Adapters). Domain and application layers must **not** import any LLM SDK, vector-store SDK, or web framework. Swapping the LLM provider, embedding model, or vector store = config + one new adapter, no changes to business logic. |

### AR-2 — Provider Abstraction

| ID     | Requirement                                                                                                    |
| ------ | -------------------------------------------------------------------------------------------------------------- |
| AR-2a  | **`ILLMProvider`** interface covering completion, streaming, and tool-calling. ≥2 working implementations: (1) **OpenAI-compatible hosted provider** (e.g., Groq), (2) **Ollama local provider**. Additional providers such as Gemini may be implemented through their own adapter (Gemini's API is not strictly OpenAI-compatible in all respects). Selected by configuration. |
| AR-2b  | **`IEmbeddingProvider`** interface covering text-to-vector embedding. Separate from `ILLMProvider` because the embedding provider may differ from the chat provider (e.g., local sentence-transformers for embeddings, hosted API for chat). ≥1 working implementation. |
| AR-2c  | **Fallback chain defined per capability**, not as one global chain. Example: chat fallback = Groq → Ollama; embedding fallback = local sentence-transformers → Gemini embeddings. A provider may support chat but not embeddings, or vice versa. |

### AR-3 — Dependency Injection

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-3  | Dependency injection throughout. FastAPI `Depends()` for request-scoped injection. Constructor injection for services. A composition root wires all dependencies. |

### AR-4 — Configuration & Prompts

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-4  | Externalised configuration via `pydantic-settings` (environment variables / `.env`). Prompts stored as versioned artifacts in `prompts/*.yaml` — never inline string literals. |

### AR-5 — Domain Errors

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-5  | Domain failures modelled as explicit typed errors (not bare exceptions or strings): `InsufficientEvidenceError`, `SafetyCheckFailedError`, `ApprovalRequiredError`, `JobNotFoundError`, etc. |

### AR-6 — Data Stores & Migrations

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-6  | **PostgreSQL** (relational) + **pgvector** (vector) with Alembic migrations. PostgreSQL is the **authoritative source of truth** for all durable state: users, documents, chunks, runs, steps, approvals, job records, traces, cost ledger. **Redis** is used for Celery broker/result transport and ephemeral state (cancellation flags, rate-limit counters, pub/sub for SSE fan-out). Redis data is treated as **ephemeral** — loss of Redis state does not lose job records or audit data. Celery/Redis result state is **non-authoritative** and may be discarded; PostgreSQL remains the source of truth for job state, workflow state, audit records, and results. |

### AR-7 — ADRs

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-7  | ≥4 Architecture Decision Records: (1) ADR-001 Chunking/Retrieval Strategy, (2) ADR-002 Orchestration Pattern, (3) ADR-003 Vector Store Choice, (4) ADR-004 Async Job Architecture (T7 central decision). |

### AR-8 — Testing

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-8  | Unit tests (domain/application, LLM stubbed); integration tests (ingestion + retrieval); contract tests (tool + agent schemas). Quality over quantity. |

### AR-9 — Packaging

| ID    | Requirement                                                                                                    |
| ----- | -------------------------------------------------------------------------------------------------------------- |
| AR-9  | `docker compose up` brings up the full system (API, Celery worker, PostgreSQL/pgvector, Redis) + a seed/ingest command. Complete `.env.example` with no real secrets. |

---

## 6. Security Requirements

### SEC-1 — OWASP Web Top 10

| ID     | Requirement                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| SEC-1a | Broken access control: object-ownership checks on runs, jobs, documents (analyst can only see own resources).  |
| SEC-1b | Cryptographic failures: passwords hashed with bcrypt/argon2; JWTs signed with a strong secret; HTTPS in production. |
| SEC-1c | Injection: parameterised SQL queries (SQLAlchemy ORM); validated file uploads (type, size, extension).         |
| SEC-1d | Rate limiting and abuse controls on all endpoints (especially LLM-calling endpoints).                         |
| SEC-1e | Security misconfiguration: proper CORS, security headers, no default credentials.                             |
| SEC-1f | Dependency scanning in CI (pip-audit/safety).                                                                 |
| SEC-1g | Auditable security logging that never records secrets, passwords, or tokens in logs.                          |

### SEC-2 — OWASP LLM Top 10

| ID     | Requirement                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| SEC-2a | **Prompt injection (direct + indirect):** Strict separation between system instructions and retrieved content. Ingested and retrieved document content is treated as **untrusted data**. Sanitisation/normalisation may remove unsafe formatting or payloads, but **does not establish trust**. System/developer instructions remain authoritative and retrieved content **cannot alter tool permissions, policies, or workflow state**. ≥3 injection cases in the eval set that the system demonstrably resists (including at least one indirect injection via poisoned document content). |
| SEC-2b | **Insecure output handling:** Never render model output as raw HTML; never pass to shell/SQL/path unvalidated; tool arguments schema-validated (Pydantic) before execution. |
| SEC-2c | **Sensitive disclosure:** PII detection/redaction via Presidio; document what data leaves infrastructure to LLM providers. |
| SEC-2d | **Excessive agency:** Per-agent tool allow-lists; no write/destructive tool without the approval gate.        |
| SEC-2e | **Unbounded consumption:** Token caps per request, iteration limits per agent, rate limits per user, payload size limits. |
| SEC-2f | **Supply chain:** Pinned dependencies, committed lockfile, scanning in CI.                                    |

### SEC-3 — Secrets

| ID     | Requirement                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| SEC-3  | No secrets in the repository — ever, including git history. Secret scanner (gitleaks/trufflehog) run over full history before submission. |

---

## 7. Engineering Process Requirements

| ID     | Requirement                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| ENG-1  | ≥30 meaningful commits across ≥6 distinct days. Conventional Commits. Atomic commits. No WIP/fix2/final-final. |
| ENG-2  | All work merges via PR — no direct pushes to `main`. ≥8 PRs with real descriptions, self-reviewed with inline comments. |
| ENG-3  | GitHub Issues linked to PRs (`Closes #N`). Board/milestones showing plan and deliberate deferrals.            |
| ENG-4  | GitHub Actions CI on every PR: build, lint (ruff), type-check (mypy), tests (pytest), dependency scan (pip-audit), secret scan (gitleaks). Green on `main`. |
| ENG-5  | Branch protection on `main`. Release tags marking increments.                                                 |
| ENG-6  | Repo hygiene: README, LICENSE, CONTRIBUTING.md, .env.example, PR/issue templates, CODEOWNERS.                 |
| ENG-7  | Agentic coding workflow documented in `docs/AGENTIC-WORKFLOW.md` with ≥5 elements.                           |
| ENG-8  | AI usage log maintained in `docs/AI-USAGE-LOG.md`: what delegated, what written, where AI misled, how verified.|

---

## 8. T7 Twist — Async Long-Running Jobs

### 8.1 Overview

All significant operations run as background jobs on a **Celery + Redis** task queue. The API returns immediately with a job ID. Progress is pushed to the client via SSE. Jobs survive worker restart, are resumable, cancellable, and idempotent.

### 8.2 Requirements

| ID     | Requirement                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| T7-01  | **Real queue + workers:** Celery workers with Redis as broker and result transport. Separate worker process(es) from the API. |
| T7-02  | **Immediate return:** API endpoints for ingestion, workflow runs, and evaluation submit to the queue and return a job ID (UUID) immediately with HTTP 202 Accepted. |
| T7-03  | **Progress push:** Job progress events pushed to the client via SSE (see FR-6 AC-6.2 for event taxonomy). Progress is informational and does not affect the job lifecycle state. |
| T7-04  | **Survive restart:** Celery `acks_late` + `reject_on_worker_lost` settings are necessary but **not sufficient**. The **PostgreSQL job record** is the authoritative state. On worker crash/restart, a recovery process queries PostgreSQL for jobs in `STARTED` state whose worker heartbeat has expired, and re-queues them. |
| T7-05  | **Resumable:** Long-running jobs (e.g., multi-document ingestion) checkpoint progress to PostgreSQL. On restart, they resume from the last checkpoint, not from scratch. |
| T7-06  | **Cancellable:** Client cancels via `POST /jobs/{id}/cancel`. The cancellation flag is set in Redis (for speed) and PostgreSQL (for durability). The worker checks the flag between steps and terminates gracefully. |
| T7-07  | **Idempotent:** Re-submitting the same job (same document, same parameters) does not create duplicate work or duplicate data. Idempotency key = `SHA-256(operation_type + canonical_input + relevant_parameters)`. Examples: ingestion = `SHA-256("ingest" + document_content_hash + ingestion_config_version)`; workflow = `SHA-256("workflow" + case_summary_hash + corpus_version + workflow_version)`. |
| T7-08  | **Job management API:** Endpoints to list jobs (filterable by status, user), get job detail/progress, cancel a job, retry a failed job. |
| T7-09  | **Job lifecycle states:** `PENDING → QUEUED → STARTED → COMPLETED | FAILED | CANCELLED`. These are **lifecycle states** persisted with timestamps in PostgreSQL. Progress information (percentage, current step, intermediate results) is emitted as **events** (via SSE) and optionally stored, but `PROGRESS` is not a lifecycle state — a job remains `STARTED` while emitting many progress events. **Retry semantics:** A `FAILED` job can be retried via `POST /jobs/{id}/retry`, which transitions it back to `QUEUED`. Each job record tracks: `attempt_number`, `max_attempts` (configurable, default: 3), `last_error`, `next_retry_at`. Automatic retries (for transient failures) use exponential backoff; manual retries are always permitted regardless of attempt count. |

---

## 9. D0 Healthcare — Domain-Specific Rules

### 9.1 Clinical Safety Rules (Business Rules)

| ID     | Rule                                                                                                          |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| BR-01  | **No dosage inference:** The system must NEVER infer, calculate, or hallucinate a medication dosage. If the exact dosage is not explicitly stated in a retrieved chunk, respond: "Dosage information not found in the available corpus. Please consult the prescribing information directly." |
| BR-02  | **No contraindication inference:** The system must NEVER infer drug interactions or contraindications beyond what is explicitly stated in retrieved chunks. Unknown interactions must be flagged, not dismissed. |
| BR-03  | **Safety Checker is mandatory:** Every clinical note workflow MUST pass through the Safety Checker agent. This step cannot be skipped or bypassed, even in degraded mode. |
| BR-04  | **Refusal over fabrication:** When in doubt, the system must refuse to answer rather than provide a plausible but unverifiable clinical claim. This is the system's primary safety behaviour. |
| BR-05  | **Citation traceability:** Every clinical claim in a drafted note must have a traceable citation to a specific chunk from a specific document. Uncitable claims are automatically **flagged** and **excluded from the approved/final note** unless explicitly reviewed and corrected by the clinician reviewer. Automatic deletion of text without clinician visibility could alter clinical meaning. |
| BR-06  | **No real patient data:** The corpus uses synthetic or public clinical data only. No real personal health information (PHI) in the repository. |

### 9.2 Clinical Note Workflow

```
┌──────────────┐    ┌───────────────────┐    ┌────────────────┐    ┌─────────────────┐    ┌───────────────┐    ┌──────────────┐
│  Case Summary │───▶│ Guideline         │───▶│ Safety Checker │───▶│ Documentation   │───▶│ PENDING       │───▶│ FINALIZE     │
│  (User Input) │    │ Researcher (AGT-01)│    │ (AGT-02)       │    │ Drafter (AGT-03)│    │ APPROVAL      │    │ NOTE         │
└──────────────┘    └───────────────────┘    └────────────────┘    └─────────────────┘    └───────────────┘    └──────────────┘
                           │                        │                       │                    │                    │
                     search_corpus            check_interactions      draft_clinical_note    Reviewer:          finalize_clinical_note
                     retrieve_drug_info       validate_dosage         (READ — draft)         ├─ Approve ───▶    (WRITE — gated)
                                                                                             ├─ Edit+Approve ▶   side effect
                                                                                             └─ Reject ──▶ END
```

> **Key distinction:** `draft_clinical_note` produces a draft (non-side-effecting). `finalize_clinical_note` persists the approved note (write/side-effecting) and only executes after human approval.

### 9.3 Dependency Direction

```
                ┌──────────────────────┐
                │      API / Workers   │  ← invokes use cases
                └──────────┬───────────┘
                           ↓
                ┌──────────────────────┐
                │    Application       │  ← defines ports (interfaces)
                │  Use Cases / Ports   │
                └──────────┬───────────┘
                           ↓
                ┌──────────────────────┐
                │       Domain         │  ← depends on nothing external
                │ Entities / Rules     │
                └──────────────────────┘
                           ↑
                ┌──────────┴───────────┐
                │      Adapters        │  ← implements ports
                │ DB / LLM / Celery    │
                │ Redis / Retrieval    │
                └──────────────────────┘
```

> **The key rule:** Domain → nothing external. Application → domain + ports. Adapters → implement ports. API/workers → invoke application use cases.

---

## 10. Out of Scope

| ID      | Item                                                                                   | Rationale                                                    |
| ------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| OOS-01  | Real clinical data / PHI                                                               | Ethical and legal constraints; synthetic data only            |
| OOS-02  | HIPAA/regulatory compliance certification                                              | Assessment scope; security controls are documented but not formally certified |
| OOS-03  | Production deployment with autoscaling                                                 | Optional deliverable; Docker Compose is the floor            |
| OOS-04  | Multi-language support beyond English                                                  | D0 does not require bilingual (that's T1)                    |
| OOS-05  | Integration with real EHR/EMR systems                                                  | Beyond assessment scope; the system operates on a standalone corpus |
| OOS-06  | Mobile-native UI                                                                       | Web UI is sufficient per requirements                        |

---

## 11. Assumptions

| ID     | Assumption                                                                                                     |
| ------ | -------------------------------------------------------------------------------------------------------------- |
| ASM-01 | Free-tier LLM APIs (Groq, Gemini) will remain available during development and evaluation.                     |
| ASM-02 | Ollama can run locally as a fallback when hosted APIs are unavailable.                                          |
| ASM-03 | A corpus of ≥30 synthetic/public clinical documents (≥150 pages) can be assembled from public sources (WHO guidelines, FDA drug labels, open-access clinical guidelines). |
| ASM-04 | The evaluator has Docker installed and can run `docker compose up` within 15 minutes.                          |
| ASM-05 | Celery + Redis is sufficient for the T7 job queue requirements; a full message broker (RabbitMQ) is not needed for MVP scope. |
| ASM-06 | PostgreSQL full-text search is adequate for the keyword/sparse retrieval component; dedicated search engines (Elasticsearch) are not needed. |

---

## 12. Risks & Mitigations

| ID     | Risk                                                    | Likelihood | Impact | Mitigation                                                                                     |
| ------ | ------------------------------------------------------- | ---------- | ------ | ---------------------------------------------------------------------------------------------- |
| RSK-01 | Free-tier LLM API rate limits hit during evaluation     | High       | Medium | Provider fallback chain defined per capability (e.g., chat: Groq → Ollama), with additional providers such as Gemini used through their own adapter where configured |
| RSK-02 | Hallucinated clinical information passes safety checks  | Medium     | High   | Structural refusal on low-confidence; Safety Checker agent; eval harness with adversarial cases |
| RSK-03 | Celery worker crashes lose job state                    | Medium     | Medium | `acks_late`, checkpointing, idempotent re-processing, persistent job state in PostgreSQL       |
| RSK-04 | Time pressure leads to incomplete documentation         | High       | High   | Documentation written in parallel with code (BRD first, ADRs with each decision, SDD last)     |
| RSK-05 | Complex async architecture introduces debugging difficulty | Medium   | Medium | Comprehensive logging, correlation IDs, job state inspection API                               |
| RSK-06 | Indirect prompt injection via ingested clinical documents | Medium    | High   | Retrieved content treated as untrusted data; sanitisation does not establish trust; system instructions authoritative; eval harness adversarial cases |

---

## 13. Traceability Matrix

> **Status key:** ✅ Implemented (code + tests + evidence) · 🔶 Partial (code exists, incomplete tests or coverage) · 🔲 Deferred (documented in SDD gap table) · ❌ Not Started
>
> This is a **living document**. Update status as implementation progresses. Do not mark ✅ merely because code exists — evidence (tests, PR links, evaluation results) must be cited.

| Req ID   | Title                                | Status     | Evidence / Notes                              |
| -------- | ------------------------------------ | ---------- | --------------------------------------------- |
| FR-1     | Document Ingestion Pipeline          | ❌ Not Started |                                            |
| FR-2     | Hybrid Retrieval & Citations         | ❌ Not Started |                                            |
| FR-3     | Evaluation Harness                   | ❌ Not Started |                                            |
| FR-4     | Multi-Agent System                   | ❌ Not Started |                                            |
| FR-5     | Orchestration & Approval Gate        | ❌ Not Started |                                            |
| FR-6     | Real-Time Streaming                  | ❌ Not Started |                                            |
| FR-7     | API Surface & UI                     | ❌ Not Started |                                            |
| FR-8     | Authentication & RBAC                | ✅ Implemented | AC-8.1: JWT (HS256, algorithm pinned on decode; signature/exp/iat/iss/aud/token-type/required-claims validated) behind `ITokenService`, bcrypt behind `IPasswordHasher` — neither library importable from domain/application (import-linter + AST boundary test). AC-8.2: three roles exactly (`analyst`/`reviewer`/`admin`), cumulative matrix in `domain/auth/permissions.py`, asserted literally and exhaustively in `tests/unit/domain/test_permissions.py`. AC-8.3: every decision made in `AuthorizationService`; identity derived only from the validated token plus the **stored** user record (role reloaded per request, so a demotion needs no token expiry); role/`user_id`/`owner_id` from body, query and headers proven inert. AC-8.4/SEC-1a: object ownership for runs/jobs/traces/sessions read from PostgreSQL via `SqlOwnershipQuery` (`SELECT user_id … WHERE id = :id`, table from an enum-keyed map, id bound, non-UUID ids miss safely); durability proven against real PostgreSQL 16 across a fresh connection pool in `tests/integration/test_sql_ownership.py`. Document ingestion is admin-only. 401 vs 403 split with a single static 401 message + `WWW-Authenticate: Bearer`. Migration `7f2a1c4b9e03` (sessions table + `user_id` indexes) verified upgrade/downgrade/re-upgrade; `addc7d39b90f` untouched; single Alembic head. In-memory adapters remain the no-database development fallback and the app refuses to start in production without `DATABASE__URL`/`AUTH__SECRET_KEY`. Approval *endpoints* are FR-5/#19, not FR-8. (#5) |
| FR-9     | Observability & Cost Accounting      | ❌ Not Started |                                            |
| AR-1     | Clean Architecture                   | ✅ Implemented | Layer boundaries enforced by import-linter (3 contracts) **and** AST boundary test, both green; RegisterDocument slice proves ports/adapters swap (#2, ADR-005) |
| AR-2     | Provider Abstraction                 | ✅ Implemented | Segregated `ILLMProvider` (completion/streaming/tool-calling) with 2 config-selected chat adapters — `GroqAdapter` (OpenAI-compatible) + `OllamaAdapter` (local) — and a separate `IEmbeddingProvider` with `LocalEmbeddingAdapter` (sentence-transformers, 384-dim); per-capability, transient-only chat fallback via `FallbackLLMProvider`, with primary and fallback order chosen in the composition root from `settings.llm.provider`/`fallback` (invalid name → typed `ProviderConfigurationError`); embeddings are single-impl by current scope (AR-2b needs ≥1, no hosted embedder configured). Provider contract + selection tests green (#7, ADR-007) |
| AR-3     | Dependency Injection                 | ✅ Implemented | Implemented for the current application surface: the composition root (`core/container.py`) constructs current adapters/providers and request dependencies bridge through `Depends()`; future adapters are wired as their tickets land (#3, ADR-006) |
| AR-4     | Configuration & Prompts              | ✅ Implemented | Nested pydantic-settings for LLM/embedding/queue/retrieval/limits/retries (secrets via env + `SecretStr`, none committed — C6); versioned `prompts/*.yaml` loaded through `IPromptProvider`/`YamlPromptProvider`, schema-validated at startup, never inline literals (#3, ADR-006) |
| AR-5     | Domain Errors                        | ✅ Implemented | Typed `DomainError`+`ApplicationError` taxonomies incl. `JobNotFoundError`/`ConfigurationError`; single type→(status,code) boundary mapping with structured `{detail,code}` body, server faults static-messaged, and a fail-safe 500 that never leaks internals (SDD A.5.1); later tickets add more typed errors (#3, ADR-006) |
| AR-6     | Data Stores & Migrations             | 🔶 Partial | PostgreSQL owns jobs/checkpoints/results; additive migration and real Redis-loss tests (#20). Other domain stores remain separate tickets. |
| AR-7     | ADRs (≥4)                            | 🔶 Partial | ADR-004/005/006/007 written; required ADR-001/002/003 remain reserved. |
| AR-8     | Testing                              | 🔶 Partial  | Unit (domain + application via fakes), integration (documents API), and architecture boundary tests; full integration/contract suite later |
| AR-9     | Packaging (docker compose)           | ❌ Not Started |                                            |
| SEC-1    | OWASP Web Top 10                     | ❌ Not Started |                                            |
| SEC-2    | OWASP LLM Top 10                     | ❌ Not Started |                                            |
| SEC-3    | Secrets Hygiene                      | ❌ Not Started |                                            |
| ENG-1    | ≥30 Commits / ≥6 Days               | 🔶 Partial  | 11 commits across 4 days so far (target ≥30 / ≥6) |
| ENG-2    | ≥8 PRs                              | ❌ Not Started |                                            |
| ENG-3    | GitHub Issues + Board                | ❌ Not Started |                                            |
| ENG-4    | GitHub Actions CI                    | ❌ Not Started |                                            |
| ENG-5    | Branch Protection                    | ❌ Not Started |                                            |
| ENG-6    | Repo Hygiene                         | 🔶 Partial  | README, .gitignore exist (empty)              |
| ENG-7    | Agentic Workflow Doc                 | ✅ Implemented | 6 categories documented in `AGENTIC-WORKFLOW.md`: instruction files, versioned prompts/skills, scoped sub-agents (security-reviewer, test-writer, doc-writer), pre-commit hooks, custom commands, versioned prompt library (#4) |
| ENG-8    | AI Usage Log                         | ✅ Implemented | `AI-USAGE-LOG.md` with 5 real entries: delegated tasks, AI mistakes (architecture violation, error leakage, false status claim, boolean edge case), verification methods (#4) |
| T7-01    | Real Queue + Workers                 | ✅ Implemented | Celery + Redis, separate processes, PostgreSQL results; real-service tests and ADR-004 (#20). |
| T7-02    | Immediate Return (HTTP 202)          | 🔶 Partial | Generic authenticated POST /jobs returns 202; domain submission routes belong to #8/#12/#17. |
| T7-03    | Progress Push (SSE)                  | ❌ Not Started |                                            |
| T7-04    | Survive Restart                      | 🔶 Partial | Hard worker death and explicit PG resume/reconcile tested; heartbeat/recovery scheduling remains #22. |
| T7-05    | Resumable                            | ✅ Implemented | Named PG checkpoints skip committed steps after restart; effect boundary documented in JOBS.md. |
| T7-06    | Cancellable                          | 🔶 Partial | Durable flag and cooperative CANCELLED outcome exist; cancel API/Redis transport remains #21. |
| T7-07    | Idempotent                           | ❌ Not Started |                                            |
| T7-08    | Job Management API                   | 🔶 Partial | Owned job detail/state/result/error polling (#20); list/cancel/retry remain later tickets. |
| T7-09    | Job Lifecycle Model                  | 🔶 Partial | Exact six-state lifecycle and illegal-transition rejection tested; FAILED retry exception remains #22. |
| BR-01    | No Dosage Inference                  | ❌ Not Started |                                            |
| BR-02    | No Contraindication Inference        | ❌ Not Started |                                            |
| BR-03    | Safety Checker Mandatory             | ❌ Not Started |                                            |
| BR-04    | Refusal Over Fabrication             | ❌ Not Started |                                            |
| BR-05    | Citation Traceability                | ❌ Not Started |                                            |
| BR-06    | No Real Patient Data                 | ✅ Implemented | Synthetic/public data policy established   |
