# SSE, Job Progress and Cancellation — Ticket 21

#21 extends the [#20 job runner](./JOBS.md), PostgreSQL store, registry, and
existing `job_events` table. Redis transports task IDs; it is not event history.
No migration is needed: #6/#20 already provide the cancellation flag, event
payload/timestamp, and `UNIQUE(job_id, sequence_number)`.

## API and authorization

| Endpoint | Behavior |
| --- | --- |
| `GET /api/v1/jobs/{job_id}/stream` | Authenticated SSE replay followed by new committed events |
| `POST /api/v1/jobs/{job_id}/cancel` | Durable cancellation; HTTP 202 with current job status |
| `POST /api/v1/jobs` | Existing admin submission; accepts registered `llm.generate` |
| `GET /api/v1/jobs/{job_id}` | Existing polling, now also exposes `cancellation_requested` |

Use the existing bearer JWT. Both new endpoints require job ownership or the
existing admin cross-owner grant. Reviewer status alone does not grant access
to another user's job. The application service reloads the stored user; supplied
roles, actor IDs and owner IDs cannot grant access. A live stream rechecks stored
identity and ownership on every page.

Authentication/authorization and the first storage read precede HTTP 200.
Missing credentials return 401; denied access 403; missing jobs 404; invalid
input 422. Job-store errors before streaming use the existing structured 503 response.
Cancellation needs no request body.

## Generation opt-in

Submit through the existing admin job endpoint:

```json
{
  "operation_type": "llm.generate",
  "payload": {
    "prompt": "Explain how a queue works.",
    "stream": true,
    "max_tokens": 1024
  }
}
```

The operation uses the configured `ILLMProvider` (`LLM__*` settings) in the
existing Celery task. `stream` defaults to false and must be a JSON boolean.
The opt-in survives redelivery in `jobs.input_payload`. False/omitted calls
`provider.complete()`; true calls `provider.stream()` and commits every text
delta before any SSE observer can receive it. The result has `text` and
`usage`; streamed results also have `finish_reason`.

Prompts contain 1–4,000 characters; `max_tokens` accepts 1–2,048 and defaults to
1,024. Output is bounded to 65,536 characters and 16,384 provider chunks.
Generation uses an asynchronous 60-second timeout and the provider timeout
option. Cleanup drains in-flight SQL before reusing the execution connection;
this is cooperative containment, not forced process termination.

Stream success requires text and an explicit provider `stop` or `length`
marker. Tool calls, oversized output, partial failures and incomplete streams
fail without a success completion event. Provider fallback is allowed before
the first chunk; a failure after output propagates instead of mixing a second
provider response into the same history.

This operation produces unverified model text. It creates no clinical note,
citations, approval, or tool execution. The grounded `/api/v1/ask` path still
validates evidence and returns cited excerpts; its raw generation is not
published as grounded evidence. The clinical agents remain unchanged.

## SSE event contract

Connect with the same bearer token:

```bash
curl -N http://localhost:8000/api/v1/jobs/<UUID>/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Last-Event-ID: 5"
```

HTTP 200 uses `text/event-stream`, `Cache-Control: no-cache, no-store`, and
`X-Accel-Buffering: no`. A connection comment is sent initially, with keep-alive
comments after 15 idle seconds. These comments have no sequence number.

Every public event has a decimal `id` equal to the persisted sequence, a named
`event`, and JSON `data` containing `job_id` and ISO `created_at`:

| Event | Additional data | Durable write |
| --- | --- | --- |
| `job_progress` | `state`, `cancellation_requested`, `attempt_number`, `error` | Job creation, state transition, cancellation request or checkpoint commit |
| `token` | `delta` | Provider text delta for an opted-in STARTED job |
| `stream_completed` | `state: "COMPLETED"`, `result` | Exactly once in the successful terminal transaction for an opted-in job |

```text
id: 6
event: token
data: {"job_id":"<UUID>","created_at":"2026-10-06T12:00:00+00:00","delta":"Hello"}

```

Progress uses #20's exact job states: `PENDING`, `QUEUED`, `STARTED`,
`COMPLETED`, `FAILED`, `CANCELLED`. There is no duplicate `SUCCEEDED` or
`PAUSED` state. Checkpoint progress adds `checkpoint_steps` and
`step_completed: true`; checkpoint values remain private.

#19 adds `workflow_id` and `workflow_state` to progress in its existing review
transactions. An `AWAITING_APPROVAL` workflow has job state `STARTED`.
Approval leaves STARTED; rejection publishes workflow REJECTED with job
COMPLETED. Observing these events never resumes work or signals finalization.

## Replay and durability

No `Last-Event-ID` means replay after sequence 0. `Last-Event-ID: N` sends only
committed public events with sequence greater than N, ordered ascending, then
polls PostgreSQL for new events every 250 ms. Cursors must be non-negative ASCII
decimal integers within the database integer range; invalid cursors return 422.
The server drains every page before closing a terminal stream.

All writers, including existing approval audit and evaluation artifact writers,
allocate sequences while holding the parent job row lock. The existing unique
constraint is the database backstop. Progress/state and successful completion
events commit together. Nothing is delivered from an uncommitted write.

Public events share the global per-job sequence with private audit/artifact
records, so gaps are valid. The stream exposes only the three public event
types; private approval/evaluation payloads remain in their authorized feature
interfaces. Job input, checkpoints, and raw exception details are not emitted.

Replay works after a process restart or Redis outage because it reads
`job_events`. An outage after SSE headers closes the connection without
inventing success. Reconnect at the last processed ID to resume.

**SSE disconnect does not cancel a job.** Connection cleanup affects only the
HTTP connection; the worker continues and missed events remain replayable.
Only the explicit cancellation command requests cancellation.

## Cancellation and races

The command locks the PostgreSQL job row, sets
`jobs.cancellation_requested=true`, and appends progress in one transaction.
The flag is authoritative; no Redis cancellation channel or Celery revoke is
required. The response may still show STARTED until an active worker stops.

If the #20 execution lock is idle, a PENDING, QUEUED, or deliberately paused
STARTED job becomes CANCELLED immediately. This does not enqueue a new task.
An active worker checks the flag before and after checkpointed steps.
Streamed generation also checks while waiting for provider chunks (250 ms
intervals) and before committing tokens. Other handlers finish their current
awaited operation before the next check. There is no forced worker termination
or general cancellation latency guarantee.

The row lock determines the winner:

- A cancellation request committed before success/failure makes that transition
  CANCELLED, with no success result or failure error.
- If COMPLETED, FAILED or CANCELLED already committed, cancellation returns that
  unchanged job. Terminal states cannot be overwritten.
- Repeated requests do not append duplicate request events.
- The worker checks cancellation and releases its advisory lock while holding
  the job row lock, including the `JobPaused` exit. A requester arriving after
  release can settle the idle job itself.

The evaluation cancel endpoint uses this same mechanism while retaining its
existing admin/operation checks. Clinical workflow phases and approval decisions
are not mutated by job cancellation; #19 already forbids approving a cancelled
review job.

## Verification and ticket boundaries

Use the existing disposable PostgreSQL/pgvector and Redis fixtures documented
in [JOBS.md](./JOBS.md#verification):

```bash
pytest tests/unit/application/test_job_streaming.py tests/unit/application/test_job_controls.py tests/unit/test_job_stream.py -q
pytest tests/integration/test_job_streaming.py tests/integration/test_approvals_api.py -q
```

Psycopg async requires a selector event loop on Windows. An equivalent command:

```powershell
python -c "import asyncio, pytest, sys; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(sys.argv[1:]))" tests/integration -q
```

Tests use real PostgreSQL transactions/locks, JWT authorization, ASGI
disconnects and separate single-slot Celery workers. Provider responses are
scripted test doubles; no live LLM quality claim is made.

#22 owns unfinished-generation replay and recovery policy. If a worker dies
after tokens commit but before its generation checkpoint, #20 redelivery can
run the unfinished step again and append more tokens. #21 preserves that
history; it adds no attempt deduplication, idempotency keys or recovery rules.
Completed steps and terminal jobs retain #20's existing replay guards.
#17 still owns clinical orchestration and consumption of #19's durable approval
signal. No orchestration, recovery, or UI implementation is included here.
