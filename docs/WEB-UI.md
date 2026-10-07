# Minimal web UI — Ticket #25

Open **http://localhost:8000/** after the normal README Docker startup and corpus
seed. The API serves the UI on the same origin. No frontend runtime dependencies,
CDN, CORS configuration, Node server, or asset build are needed. Locally,
`uvicorn app.presentation.api.app:create_app --factory --reload` serves the same UI.
Node 22 and the locked Playwright dependency are needed only to test the UI.

## Walkthrough

1. Sign in with a configured account. Development demo accounts are
   `analyst@example.com`, `reviewer@example.com`, and `admin@example.com`; their
   password is the operator's `AUTH__DEMO_PASSWORD`, never a built-in UI password.
2. Create a conversation and ask a question. Answers stream after server-side
   grounding. Expand each citation to inspect its excerpt and source metadata.
   Refusals have a separate heading and appearance, and no fabricated citations.
3. Open **Jobs & workflows**, then **Start a clinical workflow**. Enter a synthetic
   clinical question and case. Follow the durable job events and workflow phase.
   Share the displayed workflow ID with a reviewer when approval is required.
4. A reviewer/admin opens **Review**, enters that ID, and reads the draft, safety
   reasons, checked claims, and flagged/excluded/deferred claims. They can approve,
   reject with a reason, or edit and approve after inspecting the diff. A blocked
   safety verdict cannot be approved. Analysts have no approval controls.
5. After approval, select **Finalize approved note**. This requests the existing
   guarded workflow resume. The UI distinguishes the approved text from the
   immutable final note and displays the latter only when the API reads a real
   `final_clinical_notes` row. Both reviewer and workflow owner can see the result.
6. **Traces** accepts run, job, correlation and time filters. Answer/job links open
   related traces. Expand spans to see their recorded details. Cost totals retain
   the API's estimate, incomplete-usage and unavailable-cost labels.

Conversations and cited/refused answers are stored in PostgreSQL. Reloading the
page requires signing in again; selecting a saved conversation restores its
messages. Lists and histories support pagination. Bearer tokens live only in
memory and are cleared on sign-out; neither tokens nor patient text are placed
in browser storage. Use synthetic cases only.

## Streaming and failure behavior

- `POST /ask` streams the existing #24 validated fragments, **not** unverified
  provider tokens. The API commits the question/answer pair before streaming it.
- Interrupted ask delivery is marked incomplete. **Reload history** recovers the
  persisted exchange. The client never automatically repeats an ask POST.
- Job events use authenticated `fetch`, an incremental UTF-8/SSE parser, integer
  `Last-Event-ID`, and duplicate suppression. Connections retry with a capped
  10-second backoff. Navigation preserves each job's cursor for that signed-in
  page; a full reload safely replays from zero. The displayed event log keeps the
  latest 500 entries and token preview the latest 64,000 characters; durable
  events/results remain available through the API.
- Leaving a view, stopping answer delivery, closing a tab, and signing out abort
  read connections only. **Cancel job** is the sole UI cancellation command.
  Permission failures stop reconnecting; expired authentication requires sign-in.
- The persisted job GET confirms terminal state even for jobs without a terminal
  SSE frame. A completed rejection or failed workflow never becomes a final note.
- Approval conflicts retain the server's decision and reload the review. An edit
  preview preserves text and newline changes; the committed diff comes from #19.
- User/model/server strings render as text, including Markdown/HTML. Citations
  are structured text with expandable excerpts; no source URL is executed. The
  HTML response has a same-origin CSP without inline scripts or styles.

## Minimal API wiring

The web journey uses thin HTTP adapters for #17's submission/resume service and
reads #18's persisted final notes. Ticket #25 adds:

| Endpoint | Existing capability / policy |
| --- | --- |
| `POST /api/v1/runs` | `RUN_WORKFLOW`; submit registered `clinical.workflow` through `JobService`. Owner and run ID are server-derived. |
| `GET /api/v1/runs/{id}/status` | `ClinicalWorkflowService.get_workflow`; owner access plus existing reviewer/admin run access. |
| `POST /api/v1/runs/{id}/resume` | `ClinicalWorkflowService.resume_workflow`; persisted APPROVED decision and existing job/ownership checks. |
| `GET /api/v1/runs/{id}/note` | Same run authorization; read the existing #18 final-note table through the existing workflow repository. 404 until a final row exists. |

Submission fields use existing limits: question 2,000 characters, case/context
4,000 each. Edited notes remain 64,000; rejection reasons 3–4,000. Job payload
byte limits still apply. The old `GET /runs/{id}` ownership projection is unchanged.
`/ui/config` supplies the configured API prefix; UI routes are excluded from OpenAPI.

Two adapter compatibility corrections are needed: #19 stores `APPROVED` as a handoff
marker, while #17 treats approval as a decision. The workflow repository maps that
marker to `AWAITING_APPROVAL` when constructing the aggregate. It does not change
the stored decision, write a phase, or bypass the resume/finalization guards.
New workflows store an absent review as SQL NULL, rather than JSON null, so
#19's immutable-review trigger permits the first real review registration.
No orchestration, safety, approval or final-note write logic is added.

## Verification

```bash
npm ci --ignore-scripts
npm test
npx playwright install --with-deps chromium
# Set T20_TEST_DATABASE_URL and T20_TEST_REDIS_URL to disposable local services.
# The harness creates its own UUID-named schema and queue.
npm run test:browser

pytest tests/unit/presentation/test_web_ui.py -q
# Set TEST_DATABASE_URL (admin PostgreSQL URL) and T20_TEST_REDIS_URL.
pytest tests/integration/test_workflows_web_api.py -q
python -m scripts.export_openapi --check
```

On Windows, Psycopg requires the selector event loop. The browser harness sets
it automatically. To run the existing streaming/observability integration
suites on Windows, use the same policy before starting pytest:

```powershell
python -c @'
import asyncio
import pytest
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
raise SystemExit(pytest.main([
    "tests/integration/test_job_streaming.py",
    "tests/integration/test_job_observability.py", "-q",
]))
'@
```

The Playwright harness starts the real API, a separate Celery process, and the
real PostgreSQL/Redis adapters. Only model/retrieval/agent I/O is deterministic
and synthetic; approval, authorization, history, trace persistence, checkpointing,
and finalization execute their production code. A flagged review is seeded via
the existing internal approval handoff to test its review-only UI. A short
test-only packet delay proves incremental answer rendering. The edited-note
test also delays the real resume response until the worker completes, verifying
that fast finalization keeps the persisted note visible. The harness is
under `tests/web/`, not included in Docker, and registers no test HTTP routes.

`WEB_TEST_PYTHON` can select a Python executable; defaults are `.venv/bin/python`
or `.venv/Scripts/python.exe`. `WEB_TEST_PORT` defaults to 8765. Failed browser
tests retain screenshots/traces in ignored `test-results/`; no synthetic model
configuration is enabled in the application or reviewer startup path.

Known scope limits: reviewers open a shared workflow ID because the current API
has no approval-inbox listing. Provider availability and clinical answer quality
remain server responsibilities. This is a minimal functional client, with no
dashboard, user administration, corpus-upload UI or visual-polish project.
