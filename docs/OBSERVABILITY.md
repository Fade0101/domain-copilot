# Observability, correlation and cost accounting

Ticket #23 extends the original PostgreSQL `traces`, `spans` and `cost_ledger`
tables. The existing audit sink writes all execution records; no Redis event
retention or external APM is required to query them. Job progress and replay
remain in the existing `job_events` store.

## Correlation and execution coverage

All HTTP responses include `X-Correlation-ID`. A valid UUID supplied in that
request header is retained and normalized; an absent or invalid value generates
a new UUID. This value is diagnostic metadata, never an authorization credential.
Task-local context is restored after nested operations, errors and disconnects.

For a synchronous `/api/v1/ask`, the returned `trace_id` is also its `run_id`.
Retrieval, local embeddings and physical LLM calls share that trace. The enclosing
`qa.ask` span has type `request`, so a refusal without a model call is not recorded
as an LLM invocation.

Async submission persists the correlation UUID on the job. Workers restore it
from PostgreSQL, independently of the broker or current HTTP request. Each actual
worker execution has a trace and a `job.execute` span. The span records the
existing job state and outcome, including failure, cancellation and intentional
pause. Its end marks the end of that worker execution, not necessarily the end
of the workflow. A killed worker can leave an open trace; no success is inferred.
Resumed executions create another trace under the same job/correlation identity.

The async `run_id` is the explicit workflow ID when present, otherwise the job's
persisted correlation UUID (the existing clinical workflow's default identity).
Use `job_id` to select precisely one job. Agents share their execution trace.
Clinical tools and their retrieval/ask calls retain their existing evidence
trace IDs, all linked by run, job, owner and correlation. Tool arguments, clinical
draft text, completion prompts and completion text are not added to provider or
agent spans. Existing authorized retrieval telemetry remains available.

## Query API

All routes below use the existing bearer-token authentication and persisted
ownership rules. Users can see their own traces and usage. Admin cross-owner
trace access uses `VIEW_SYSTEM_HEALTH`; cross-owner cost queries use
`VIEW_COST_DASHBOARD`. Services reload the stored user, so a previously issued
admin token or stale `Principal` does not retain revoked privileges.

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/traces` | Trace metadata in `items`, with `limit` and `offset` |
| `GET /api/v1/traces/{trace_id}/spans` | Trace metadata and a page of spans |
| `GET /api/v1/usage` | Ledger totals and a provider/model breakdown |

The existing `GET /api/v1/traces/{trace_id}` ownership projection is preserved.

List and usage filters are `run_id`, `job_id`, `correlation_id`, `user_id`,
`start_time` and `end_time`. Filters are combined with AND. Times must include a
timezone. Ranges include the start and exclude the end. Trace time filters use
trace start time; usage time filters use the ledger entry's recording time.
Without a user filter, non-admin queries are restricted to the caller.

Trace pages default to 50 items, span pages to 100, both capped at 200. Traces
sort by newest start time and ID; spans sort by earliest start time and ID.
The usage response aggregates **all** matching ledger entries, independently of
`limit`/`offset`. These responses carry `Cache-Control: no-store`.

Examples, with a token obtained from `/api/v1/auth/token`:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  -H "X-Correlation-ID: 7903480a-bb6a-4c44-86ee-a3bd81a97109" \
  -H 'Content-Type: application/json' \
  -d '{"question":"When does the synthetic clinic open?"}' \
  http://localhost:8000/api/v1/ask

curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/api/v1/traces?correlation_id=7903480a-bb6a-4c44-86ee-a3bd81a97109'

curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/usage?job_id=$JOB_ID"
```

## Usage and estimates

Each physical LLM call records the actual provider adapter, returned model when
available, and provider-reported prompt/completion/total token counts. Fallback
attempts are separate records under their respective providers. A streamed call
records usage once on completion/closure, using the final cumulative counters.
Repeated usage chunks are not summed. Failed or cancelled streams may retain
partial counts, but cannot claim complete usage or a complete cost estimate.
Checkpoint reuse does not generate another model call or ledger entry.

Local embedding calls record their provider/model and explicitly unavailable
token and compute costs. Local inference is not assumed to be free.

Set `OBSERVABILITY__RATES` to a JSON list. Each entry contains:

```json
{
  "provider": "your-provider",
  "model": "your-model",
  "prompt_per_million": 2.0,
  "completion_per_million": 4.0,
  "source": "your-verified-pricing-schedule-and-version"
}
```

These numbers are illustrative, **not a real provider's prices**. The default is
`[]`. Configure rates verified for your deployment. API and worker receive the
same settings through the existing Compose environment. Matching uses the exact
provider/model pair; an unmatched model has unavailable cost. Duplicate rate
pairs, missing sources and invalid amounts fail configuration validation.

Computed amounts are in USD and always estimates, never invoices. The rate
source and rate values are persisted with each ledger entry; changing settings
does not reprice previous calls. Span and ledger insertion share one transaction,
and the unique ledger `span_id` prevents duplicate accounting for the same call.

The usage response distinguishes:

- `tokens_prompt`, `tokens_completion`, `total_tokens`: sums of known counters.
- `usage_complete`: false if any call has missing/incomplete counters, or no calls
  are recorded.
- `known_estimated_cost`: the subtotal for calls with a usable rate and counters.
- `estimated_cost`: null if any matching call's cost is unavailable.
- `cost_complete`, `cost_status`, `is_estimate`, and a notice explaining limitations.

The breakdown includes counts of calls with unavailable usage/cost. Missing data
is not silently turned into a free call. Totals cover captured calls, not cloud
invoices, infrastructure spend, unreported provider work or local compute.

## Liveness and readiness

`GET /health` returns 200 with `{"status":"ok"}` when the process can serve a
request; it does not depend on external services.

`GET /ready` probes dependencies concurrently and returns 200 only when all
configured probes pass. Otherwise it returns 503 with per-dependency `ok`,
`unavailable` or `timeout` statuses. Error messages and credentials are not exposed.

| Dependency | Probe |
| --- | --- |
| PostgreSQL | Real database query (`SELECT 1`) |
| Redis | Real broker `PING` |
| Local embeddings | A small fixed input through the configured embedding model |
| Groq | Authenticated model metadata access and active-model check |
| Ollama | `/api/show` confirms that the configured model is installed |

Chat probes do not generate billable completions. They establish reachability,
credentials and model access, not a guarantee that every later inference succeeds.
Both primary and configured fallback must pass. Set `LLM__FALLBACK=` to disable an
unused fallback. A cold embedding model can initially time out; repeated polls
reuse the pending model probe instead of queuing unbounded work.
`OBSERVABILITY__READINESS_TIMEOUT_SECONDS` bounds each probe (default 3 seconds).

## Persistence and operational limits

Run `alembic upgrade head` before using the trace/accounting API. Migration
`d23b7e9a0142` adds run/query indexes, span timestamps and ledger attribution/rate
fields. It makes cost/token counters nullable to represent unknown quantities.
Existing records are retained; missing historical provenance cannot be
reconstructed or promoted into verified accounting. Downgrade refuses to convert
unknown counters/costs into the previous NOT NULL schema.

Trace writes retain the existing bounded, best-effort audit policy: database
failure does not turn a valid clinical answer into a failed request. A static
warning and the audit event remain in the existing log. Trace queries fail with
503 if their store is unavailable. This ticket does not implement recovery of
unwritten telemetry, external billing reconciliation, retention policy,
dashboards or alerts. The UI in #25 can consume the query APIs directly.

Tests use the repository's existing PostgreSQL/Redis fixtures and scratch
databases/schemas. No ticket-specific services or new Docker stack are added.
