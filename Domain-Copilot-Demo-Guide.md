# Domain Copilot — Comprehensive Video Demo & Presenter Guide

> **Confidential Presenter Notes** — Kept outside the repository git history.  
> **Security & Privacy Reminder**: Use **only synthetic cases**. Keep `.env`, passwords, Groq API keys, and JWT bearer tokens off the recorded screen.

---

## 1. Quick Reference & Credentials Cheat Sheet

| Parameter | Value | Notes |
| :--- | :--- | :--- |
| **Workspace Directory** | `E:\Domain Copilot\domain-copilot` | Primary workspace |
| **Target Branch** | `dev` | Fully updated with Tickets #24, #25, #26, #27 |
| **Web UI URL** | `http://localhost:8000/` | Minimal, high-performance vanilla web client |
| **Health Check** | `http://localhost:8000/health` | Liveness check (JSON) |
| **Readiness Check** | `http://localhost:8000/ready` | Dependency health check (PostgreSQL, Redis, Ollama model probe) |
| **Analyst Account** | `analyst@example.com` | Permissions: Conversations, Grounded Q&A, Launch Workflows |
| **Reviewer Account** | `reviewer@example.com` | Permissions: Review Drafts, Edit, Approve, Finalize Notes |
| **Admin Account** | `admin@example.com` | Permissions: Full system & review access |
| **Demo Password** | Value of `AUTH__DEMO_PASSWORD` in `.env` | Shared locally across all seeded demo accounts |
| **LLM Provider / Model** | `ollama` / `qwen2.5:1.5b` | Local Ollama instance (100% offline, zero API quota issues) |
| **Reranker Model** | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Lightweight (<300MB), ultra-fast CPU inference |
| **Model Cache Path** | `E:/Domain Copilot/domain-copilot/.artifacts/huggingface` | Persistent on Drive E (avoids filling C: drive) |

---

## 2. Pre-Flight Checklist & Setup (Before Recording)

### Step 2.1 — Verify Local Configuration
1. Open PowerShell and navigate to the project directory:
   ```powershell
   cd "E:\Domain Copilot\domain-copilot"
   git status
   ```
   *Expected output*: `On branch dev. Your branch is up to date with 'origin/dev'. working tree clean`.

2. Inspect `.env` locally:
   - `AUTH__DEMO_PASSWORD` has a generated password (e.g. `jVhzN98K6a6jeJv2FxSflEea`). Copy it to your clipboard.
   - `LLM__PROVIDER=ollama`
   - `LLM__MODEL=qwen2.5:1.5b`
   - `LLM__OLLAMA_BASE_URL=http://host.docker.internal:11434`
   - `MODEL_CACHE_PATH=E:/Domain Copilot/domain-copilot/.artifacts/huggingface`

### Step 2.2 — Start Services via Docker Compose
In your terminal, launch the full stack:
```powershell
docker compose up -d
docker compose ps
```
*Expected running services*:
- `postgres` (port 5433:5432, healthy)
- `redis` (port 6379, healthy)
- `api` (port 8000, healthy)
- `worker` (background processing)
- `migrate` (exited 0 — successful Alembic schema migrations)
- `seed` (exited 0 — seeded demo accounts and synthetic healthcare corpus)

### Step 2.3 — Verify System Readiness
Open your browser and verify:
1. `http://localhost:8000/health` → `{"status": "ok"}`
2. `http://localhost:8000/ready` → `HTTP 200` with status `"ready"` and `checks.llm_primary == true`.

### Step 2.4 — Warm the Reranker Model (One-Time Rehearsal)
> **Why?** The first search query initializes the lightweight cross-encoder into memory. Do this **once** before recording so your video is snappy and has zero lag!

1. Open `http://localhost:8000/`
2. Sign in as **`analyst@example.com`** using your local demo password.
3. Click **New conversation**, title it `Warming rehearsal`.
4. Ask:
   ```text
   What states does the training vocabulary distinguish in allergy-status documentation?
   ```
5. Wait for the streamed answer and citations to appear (usually ~2–3 seconds). Once citations load, the models are cached and ready!

---

## 3. Step-by-Step Video Recording Script (10–12 Minutes)

### Scene 1: Introduction & System Architecture (1 Minute)
* **Visual**: Show the browser at `http://localhost:8000/` and terminal showing `docker compose ps`.
* **What to Say (Narration)**:
  > *"Welcome to the demonstration of Domain Copilot — an enterprise-grade clinical documentation assistant. The platform is built strictly on Clean Architecture principles, ensuring complete domain isolation, port-adapter boundaries, durable async workflows, deterministic safety guardrails, and human-in-the-loop governance.*
  > 
  > *All data in this demonstration is strictly synthetic, adhering to our healthcare data minimization guidelines (BR-06). Let's step into the system through our role-based access control."*

---

### Scene 2: Analyst Persona — Grounded Conversations & Streaming (3 Minutes)

#### A. Sign In & Role-Based UI
1. On `http://localhost:8000/`:
   - Email: `analyst@example.com`
   - Password: `<your AUTH__DEMO_PASSWORD>`
   - Click **Sign In**.
2. **Point out**:
   - The navigation shows **Conversations**, **Jobs & Workflows**, and **Observability**.
   - Note that there is **no Review tab** visible — Analysts are cryptographically restricted from approving or finalizing clinical records.

#### B. Grounded Evidence Retrieval & Live Token Streaming
1. Click **+ New conversation**, enter title: `Clinical Policy Inquiry`.
2. Enter the prompt:
   ```text
   What states does the training vocabulary distinguish in allergy-status documentation?
   ```
   *(Alternative tested prompt: `What does a training handoff separate in closed-loop handoff documentation?`)*
3. Click **Send** (or press Enter).
4. **Point out as it streams**:
   - Live server-sent token streaming from local Ollama.
   - The response is grounded directly against the ingested synthetic knowledge corpus (`allergy-status-capture.md`).
   - Click to expand the **Citations** tray below the answer: show the verbatim excerpt (`Distinct states`), document title, and relevance score (>0.94).

#### C. Safety Refusal & Hallucination Prevention
1. In the same conversation, ask an unsupported clinical action:
   ```text
   What exact insulin dose should be prescribed for a patient?
   ```
2. Click **Send**.
3. **Point out**:
   - The model gracefully refuses to guess or hallucinate dosages: *"Not enough information in the corpus"*.
   - Explain: *"Domain Copilot enforces deterministic grounding. If evidence is missing or out of scope, the system fails closed rather than inventing medical guidance."*

#### D. Conversation Persistence
1. Refresh the browser page (`F5`).
2. Sign in again as `analyst@example.com`.
3. Select `Clinical Policy Inquiry` from the sidebar: show that both exchanges and their citations were persisted securely in PostgreSQL.

---

### Scene 3: Orchestrator & Durable Workflows (2.5 Minutes)

#### A. Starting a Multi-Phase Clinical Workflow
1. Click **Jobs & Workflows** in the top navigation.
2. Under **Start a clinical workflow**, enter:
   - **Clinical question**:
     ```text
     What states does the training vocabulary distinguish in allergy-status documentation?
     ```
   - **Synthetic case summary**:
     ```text
     Synthetic clinical encounter. Review allergy record documentation requirements.
     ```
3. Click **Start Clinical Workflow**.

#### B. Real-Time Phase Progression & Event Stream
1. Watch the live workflow display:
   - Point out the **Job ID**, **Correlation ID**, and status badge.
   - Highlight the phase transitions updating via SSE:
     `RESEARCH` ➔ `SAFETY_CHECK` ➔ `DRAFT` ➔ `AWAITING_APPROVAL`.
2. **Copy the Workflow ID / Job ID** displayed on screen.
3. **Demonstrate Stream Resilience**:
   - While the job is running (or once awaiting approval), click over to **Conversations**, then switch back to **Jobs & Workflows**.
   - Point out that all progress events replayed cleanly from the event store without duplicate entries.
   - Explain: *"Navigation or browser disconnects do not cancel background jobs. Jobs run reliably on our Celery-compatible queue backed by Redis and PostgreSQL."*

---

### Scene 4: Reviewer Persona — Human-in-the-Loop & Finalization (3.5 Minutes)

#### A. Reviewer Sign-In (Use an Incognito / Second Browser Window)
1. Open a new Incognito / Private window and navigate to `http://localhost:8000/`.
2. Sign in as:
   - Email: `reviewer@example.com`
   - Password: `<your AUTH__DEMO_PASSWORD>`
3. **Point out**:
   - The **Review** tab is now visible in the top navigation bar because the user has the `reviewer` role.

#### B. Loading and Inspecting the Pending Review
1. Click **Review**.
2. Paste the **Workflow ID** copied from the Analyst session.
3. Click **Load review**.
4. **Inspect the draft**:
   - Review the generated clinical draft.
   - Expand the **Evidence Citations** and **Safety Verdict**.
   - Point out that the safety checker ran automated checks on claims and flagged zero hallucinations.

#### C. Clinician Edit & Difference Tracking
1. Click **Edit and approve**.
2. In the editable note box, add a clear documentation statement, for example at the bottom:
   ```text
   Note: All unprovided clinical parameters remain verified as unavailable.
   ```
3. Point out the live diff comparison highlighting added/modified text.
4. Click **Approve edited note**.
5. Explain: *"In Domain Copilot, approval is an audit-locked milestone, but approval alone is NOT finalization."*

#### D. Note Finalization
1. Click **Finalize approved note**.
2. Show the resulting banner:
   - **Finalized Clinical Note** with its immutable Note ID and Approval ID.
   - Explain: *"Finalization seals the note against further edits, writes the full clinical audit record to PostgreSQL, and enforces fail-closed clinical safety invariants."*

#### E. Cross-Persona Synchronization
1. Switch back to the **Analyst** browser window.
2. In the Jobs tab, reload or observe the workflow status: show that the workflow now reflects `COMPLETED` and displays the finalized note.

---

### Scene 5: Observability, Traces & Cost Accounting (1.5 Minutes)

1. Click **Observability** in the navigation bar.
2. View the recent traces:
   - Filter by the **Correlation ID** from your workflow.
   - Expand a span: show the breakdown across workflow execution phases (`research_guidelines`, `safety_check`, `draft_documentation`).
   - Point out duration, latency metrics, and model calls.
3. **Explain Cost Honesty**:
   - *"Domain Copilot implements rigorous cost honesty. If a pricing schedule is configured, estimates are calculated transparently. When pricing rates are not configured, cost is honestly displayed as unavailable rather than displaying misleading zeros."*

---

### Scene 6: Security Hardening & Edge Controls (1 Minute Wrap-Up)

Briefly highlight the security hardening delivered under Ticket #26:
1. **Raw ASGI Request Size Limits**:
   - An outermost ASGI middleware enforces a strict 1MB payload limit on all JSON routes to prevent memory exhaustion, while allowing streaming uploads on `/api/v1/documents/ingest`.
2. **Deterministic PII/PHI Redaction (ADR-009)**:
   - In-tree deterministic redaction with NHS Number Modulus 11 validation, SSN, MRN, phone, and email masking.
3. **Dependency Integrity**:
   - Exact-pinned dependencies with 0 known vulnerabilities via `pip-audit`.
4. **Git History Hygiene**:
   - Clean git history verified by Gitleaks scan with 0 exposed secrets.

---

## 4. Presenter FAQs & Edge-Case Handling

| Question / Situation | Recommended Presenter Response |
| :--- | :--- |
| **Where does data persist?** | All jobs, workflows, conversation history, audit checkpoints, and finalized notes persist in **PostgreSQL**. **Redis** manages queues and ephemeral pub/sub. |
| **What happens if a user closes the browser during a run?** | The background worker continues execution unaffected. Opening the workflow page reconnects to SSE and replays past events from the database. |
| **Can an Analyst bypass the review gate?** | No. The backend strictly checks JWT claims and rejects unauthorized approval/finalization attempts with `HTTP 403 Forbidden`. |
| **Why did a prompt return "Not enough information in the corpus"?** | Domain Copilot implements fail-closed deterministic safety (AR-2, BR-06). If a question is phrased too broadly and retrieves document disclaimer headers rather than the specific factual section, the system refuses rather than guessing or hallucinating. Specific topical questions (e.g. asking about vocabulary states or handoff contents) produce exact verbatim citations. |
| **Why are we using local Ollama (`qwen2.5:1.5b`)?** | Local Ollama eliminates all third-party cloud API rate limits, transient network outages, and sudden external model deprecations, ensuring 100% reproducible and offline demo execution. |
| **How fast is the reranker?** | The lightweight `cross-encoder/ms-marco-MiniLM-L-6-v2` (<300MB) runs CPU inference in <1.5 seconds without consuming gigabytes of Docker memory. |
| **Can a note be finalized without approval?** | No. Invariant checks require a verified approval record before finalization can occur. |

---

## 5. Quick Recovery Commands (Keep in a Terminal Tab)

```powershell
# Check running containers
docker compose ps

# View real-time logs from API and Worker
docker compose logs -f --tail=50 api worker

# Re-run migrations or seeds if necessary
docker compose run --rm migrate
docker compose run --rm seed

# Graceful restart preserving volumes and data
docker compose restart api worker
```
