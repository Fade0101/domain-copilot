# HTTP API contracts — Ticket #24

The running application publishes OpenAPI 3.1 at `/openapi.json`, Swagger UI at
`/docs`, and ReDoc at `/redoc`. The checked-in default contract is
[openapi.json](openapi.json). Paths below use the default `/api/v1` prefix;
the runtime specification follows a configured prefix.

Regenerate with `python -m scripts.export_openapi`. Run
`python -m scripts.export_openapi --check` to detect drift. CI runs the check.
Export builds the route graph without starting dependencies or loading model weights.
The schemas, defaults, bounds, response media types, bearer scheme, role notes and
SSE data schemas come from the application. Ticket #27 owns broader contract-test
execution; #24 includes focused acceptance and publication checks.

## Dependency inspection

This implementation starts at `3d37fe3`, the merged #17 orchestrator. #5 provides
stored-role JWT resolution and ownership policy; #10 provides the exact seven-field
citation and grounded answer/refusal; #13 contains untrusted prompt/evidence input.
#14 → #15 → #16 remains the research/safety/draft pipeline. #17 owns orchestration,
#19 owns human decisions, and #18 owns guarded finalization.

#20 provides JobService, the job store and queue. #21 provides ordered SSE replay
and cooperative cancellation. #22 provides the locked, audited FAILED retry,
phase checks and preserved checkpoints/accounting. The new retry route calls that
existing transaction; it adds no scheduler, lease, backoff or recovery mechanism.

There is **no dependency on unmerged #23 code**. No observability implementation,
port or placeholder is added. Existing trace IDs are copied unchanged. Integration
with #23 should require only its normal composition/schema updates.

## Endpoint and authorization matrix

Every protected request uses `Authorization: Bearer <JWT>`. The stored user and
current role are authoritative; tokens and role/owner-like request fields cannot
grant permissions. Unknown resources return 404; existing foreign resources return
403 under #5's established policy. Owners retain their actual user ID in responses
even when an administrator accesses their jobs.

| Method and path | Request | Successful response | Roles / ownership |
| --- | --- | --- | --- |
| POST /evaluations | No body; server pins the configured evaluation catalog | 202 JobAcceptedResponse; Location = status_url | Admin only; job belongs to caller |
| GET /jobs | state optional (JobState); limit 1–100, default 50; offset ≥0, default 0 | 200 JobListResponse {items, limit, offset} | Analyst/reviewer: own jobs; admin: all owned jobs |
| GET /jobs/{job_id} | Resource ID | 200 JobStatusResponse | Owner or admin |
| POST /jobs/{job_id}/retry | {reason: string}, 3–4000 characters after trimming; extra fields forbidden | 202 JobAcceptedResponse; Location = status_url | Admin only; fresh persisted role checked again in the retry transaction |
| POST /jobs/{job_id}/cancel | No body | 202 JobStatusResponse | Owner or admin |
| GET /jobs/{job_id}/events | UUID; optional Last-Event-ID | 200 text/event-stream | Owner or admin, rechecked on every page |
| GET /jobs/{job_id}/stream | Same as /events | Same SSE contract; existing compatible alias | Owner or admin |
| POST /ask | {question, stream?: false, session_id?: null}; extra fields forbidden | 200 AnswerResponse or RefusalResponse; JSON or SSE selected by stream | Every role; any session must belong to caller |
| POST /sessions | {title: string}, 1–255 characters after trimming | 201 SessionResponse; Location points to the session resource | Every role; caller becomes owner |
| GET /sessions | limit 1–100, default 50; offset ≥0, default 0 | 200 SessionListResponse | Own sessions only, including for admin |
| GET /sessions/{session_id} | Resource ID | 200 ResourceAccessResponse {resource_type, resource_id, owner_id} | Existing #5 access projection; own session only for every role |
| GET /sessions/{session_id}/messages | UUID; limit 1–100, default 50; offset ≥0, default 0 | 200 MessageListResponse | Own session only for every role |

Job/session lists filter ownership **before** pagination and sort by created_at
descending, then ID descending. Offset pagination is a live view: concurrent inserts
can shift later pages. Message history sorts by ascending committed sequence.
A page can split an exchange, but both messages commit atomically. There is no
client endpoint for inserting assistant evidence.

The existing evaluation status/report/restart, generic admin job submission,
authentication, ingestion and approval routes remain published. Evaluation restart
is distinct from job retry: its existing service can start another evaluation job.
Generic job submission remains admin-only. Reviewer/admin may review all runs via
`VIEW_ALL_RUNS`; this does **not** grant reviewers access to foreign jobs or sessions.

## Answers and citations

Question length is 1–2000 characters after trimming, as in #10.
`stream` is a strict JSON boolean. With `stream=false` (default):

```json
{
  "answer": "[1] Complete source excerpt",
  "citations": [{
    "document_id": "00000000-0000-0000-0000-000000000001",
    "document_name": "synthetic.md",
    "section": null,
    "page": null,
    "chunk_id": "00000000-0000-0000-0000-000000000002",
    "relevance_score": 0.9,
    "text_snippet": "Complete source excerpt"
  }],
  "refused": false,
  "trace_id": "00000000-0000-0000-0000-000000000003"
}
```

Citation keys are **exactly** `document_id`, `document_name`, `section`, `page`,
`chunk_id`, `relevance_score`, `text_snippet`. IDs are UUIDs; section/page are
required nullable fields; score is finite and within [0, 1]. Provenance and scores
come from indexed retrieval, never client-supplied or model-invented metadata.
A successful answer requires at least one citation.

Refusal is a distinct discriminated schema with HTTP **200**:

```json
{
  "answer": "Not enough information in the corpus",
  "citations": [],
  "refused": true,
  "trace_id": "00000000-0000-0000-0000-000000000003"
}
```

The refusal text has no trailing period. OpenAPI uses `refused` as the
discriminator; refusal citations have maximum length zero. Empty, conflicting,
unsupported or contaminated evidence keeps #10/#13's fail-closed behavior.
Provider failure returns 503, not a refusal.

## SSE wire contracts

Job SSE uses #21's existing reader. Each event has a per-job integer `id`, named
`event`, JSON `data` and a blank line separator. `Last-Event-ID` resumes strictly
after that sequence; absent/empty means 0. Accepted values are ASCII decimal
0–2147483647 (at most ten digits); malformed/out-of-range values return 422.
Unknown jobs return 404, foreign jobs 403, before HTTP 200 starts.

| Job event | JSON data |
| --- | --- |
| job_progress | job_id, created_at, state, cancellation_requested, attempt_number, error; operation-specific public progress fields may also appear |
| token | job_id, created_at, delta |
| stream_completed | job_id, created_at, state, result |

OpenAPI `x-sse-events` references named data schemas for each event.
`: connected` and `: keep-alive` are comments, not data events. Terminal jobs drain
their remaining events and close. Post-header storage or authorization failures
close without inventing a terminal event. Reconnect with the last received ID.
The stream never exposes private approval audit/signal payloads. Disconnect does
not cancel a job; use the cancel command. Repeating cancel on a terminal job is a
successful no-op; a running worker stops cooperatively.

Ask SSE is a synchronous, non-durable response chosen by `stream=true`.
The complete #10 result is grounded before headers and before any text is emitted.
This deliberately buffers the evidence selector; deltas are fragments of the
validated source quotations, not raw LLM tokens. Successful answers emit `token`
data {delta, trace_id}, then `stream_completed` containing the complete
AnswerResponse. Concatenating deltas reproduces `answer` exactly. Refusals emit
one `refusal` event containing RefusalResponse and close, with no text deltas.
Ask streams have no replay IDs or job cancellation semantics. Both streams use
`text/event-stream`, `Cache-Control: no-cache, no-store` and
`X-Accel-Buffering: no`.

## Errors and state semantics

All HTTP errors, including framework validation, 404 and 405, use
`ErrorResponse = {detail: string, code: string}`. Validation returns the static
`VALIDATION_ERROR` message without echoing passwords, question text or other input.
Application/domain error codes and status mappings are preserved.

| Status | Codes / meaning |
| --- | --- |
| 400 | DOMAIN_ERROR, APPLICATION_ERROR; malformed operations |
| 401 | NOT_AUTHENTICATED; WWW-Authenticate: Bearer |
| 403 | PERMISSION_DENIED, RESOURCE_FORBIDDEN |
| 404 | RESOURCE_NOT_FOUND (application), HTTP_ERROR (unknown route) |
| 405 | HTTP_ERROR |
| 409 | INVALID_STATE_TRANSITION; retry/approval/lifecycle conflict |
| 413 | UPLOAD_TOO_LARGE |
| 422 | VALIDATION_ERROR, INVARIANT_VIOLATION |
| 500 | CONFIGURATION_ERROR, INTERNAL_ERROR; static public detail |
| 503 | JOB_STORE_UNAVAILABLE, KNOWLEDGE_UNAVAILABLE, APPROVAL_STORE_UNAVAILABLE, HISTORY_STORE_UNAVAILABLE |

Job state remains PENDING / QUEUED / STARTED / COMPLETED / FAILED / CANCELLED.
`JobStatusResponse.result` and `error` are nullable. Inputs, checkpoints and
internal lease data are not exposed. Job identity and checkpoints survive manual
retry. A second competing or repeated retry after acceptance returns 409; it does
not reset the retry budget or create a second logical job. Ineligible workflow
phases and cancellation remain authoritative.

Clinical workflow remains:
RESEARCH → SAFETY_CHECK → DRAFT → AWAITING_APPROVAL → FINALIZE → COMPLETED.
APPROVED is a human decision, not a workflow state. API contract work neither
approves side effects nor calls the finalizer. The #17 orchestrator and #18 guard
remain authoritative.

## Persistence and compatibility

Migration `d24a1b9c830f` follows `c22a4b8f901d` and adds only
`session_messages`. Existing `sessions` has owner/title/time but no message
storage, so it cannot implement persistent history without this addition.
The new table stores user content and the exact grounded assistant outcome.
It has a session foreign key, unique per-session sequence and role/payload checks.
A session-row lock serializes concurrent two-message appends. A failed insert
rolls back the whole exchange. Downgrade refuses to discard nonempty history.

Plain ask remains stateless unless session_id is provided. History writes finish
before successful JSON/SSE is returned, so disconnecting after delivery starts
does not remove stored messages. Repeating an ask is a new exchange; this API
does not claim request idempotency for synchronous asks. History is not supplied
to retrieval or the model as clinical evidence. No session delete/edit or
clinical-note write operation is introduced.

The intentional client-visible validation change is that 422 framework failures
now use the common typed envelope instead of FastAPI's default error list.
Existing ask JSON fields and the existing job /stream route are preserved.

## Acceptance evidence

| Acceptance criterion | Evidence |
| --- | --- |
| Required endpoints published with contracts | tests/unit/test_api_contracts.py checks paths, request/response schemas, media types, bearer scheme and role notes; runtime-generated snapshot and CI drift check |
| Exact citations and distinct refusals | Real AskUseCase through HTTP with deterministic ports, JSON/SSE parity, citation bounds, refusal and injection cases |
| Typed errors; refusal distinct from 5xx | Validation/auth/framework error checks; provider outage is sanitized 503 before SSE headers |
| Role/ownership enforcement | Role matrix tests for evaluation, list/retry/cancel/events; PostgreSQL JWT history tests deny reviewer/admin foreign-session access |
| Runtime persistence and safety | tests/integration/test_api_history.py verifies new-pool reads, concurrent atomic pairs, rollback, competing retry audit and AWAITING_APPROVAL rejection |
