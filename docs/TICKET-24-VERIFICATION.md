# Ticket #24 implementation and verification

Base: `3d37fe3` (merged Ticket #17). Branch: `feat/ticket-24-api-contracts`.
Worktree: `.worktrees/ticket-24-api-contracts`. No #23 commits were merged or
cherry-picked. No #23 worktree files were changed.

## Acceptance criteria

All five criteria are implemented and covered by concrete evidence:

1. Required evaluation, job, ask, session and message-history endpoints have
   published request/response contracts. Runtime OpenAPI 3.1.0 contains 29 paths;
   all 399 internal schema references resolve.
2. Answer and refusal are distinct schemas, discriminated by `refused`.
   Citations retain exactly #10's seven fields. Refusal is HTTP 200.
3. Domain/application, framework validation and HTTP errors share `{detail, code}`.
   Provider/storage failures remain sanitized 5xx responses.
4. #5's stored-role and ownership rules are both documented and enforced.
   Sessions remain owner-only for all roles; retries remain admin-only.
5. The committed OpenAPI is generated from runtime routes, with a CI drift check.
   Export checks passed on Windows and Linux.

See [API-CONTRACTS.md](API-CONTRACTS.md) for endpoint contracts, SSE framing,
ownership, compatibility notes and requirement-to-component mapping.

## Files and justification

| Added files | Purpose |
| --- | --- |
| app/domain/sessions.py | Pure session metadata and title invariant |
| app/application/ports/sessions.py | Message DTO and durable history port |
| app/application/sessions.py | Session authorization and atomic grounded-ask history use cases |
| app/infrastructure/persistence/sql/session_store.py | PostgreSQL adapter using existing database/session ownership |
| app/presentation/api/ask_stream.py | Serialize already-grounded text and refusal into SSE |
| app/presentation/api/openapi.py | Error/media/SSE/role/ownership OpenAPI metadata |
| app/presentation/api/routes/sessions.py | Create/list sessions and read ordered messages |
| app/presentation/api/schemas/errors.py | Shared typed error envelope |
| app/presentation/api/schemas/events.py | Published SSE JSON data schemas |
| app/presentation/api/schemas/sessions.py | Session and history HTTP contracts |
| migrations/versions/d24a1b9c830f_session_messages.py | Add message storage absent from existing schema |
| scripts/export_openapi.py | Deterministic export and drift check |
| tests/support/session_fakes.py | Deterministic history port double |
| tests/unit/test_api_contracts.py | 51 focused acceptance tests |
| tests/integration/test_api_history.py | 8 PostgreSQL/Redis acceptance tests |
| docs/openapi.json | Published generated default contract |
| docs/API-CONTRACTS.md | Consumer contract and architectural mapping |
| docs/TICKET-24-VERIFICATION.md | This implementation report and exact validation evidence |

| Modified files | Purpose |
| --- | --- |
| .github/workflows/ci.yml | Fail CI on stale published OpenAPI |
| README.md | Link contract documentation and publication |
| app/application/jobs/service.py | Authorized listing and adapter to #22's locked retry |
| app/application/ports/jobs.py | Add filtered list query to existing store |
| app/core/container.py | Wire history adapter/use cases in existing composition root |
| app/infrastructure/persistence/job_store.py | Owner-filtered SQL job list |
| app/infrastructure/persistence/models.py | Mirror additive session-message migration |
| app/presentation/api/app.py | Register session router and common OpenAPI responses |
| app/presentation/api/dependencies.py | Session-service dependency provider |
| app/presentation/api/errors.py | Normalize framework errors; map unavailable history |
| app/presentation/api/routes/evaluations.py | Document body-free catalog-pinned submission |
| app/presentation/api/routes/jobs.py | List/retry routes and compatible /events alias |
| app/presentation/api/routes/knowledge.py | JSON/SSE outcomes and optional session persistence |
| app/presentation/api/schemas/jobs.py | List and retry contracts |
| app/presentation/api/schemas/knowledge.py | Strict stream flag, optional session, discriminated outcomes |
| tests/support/job_fakes.py | Implement added list query on existing job double |

## Reuse and dependency safety

Reused #5's authorization/user/ownership services, #10's AskUseCase and Citation,
#20's JobService/IJobStore/queue, #21's SSE reader and cancellation, and #22's
execution lock, retry transaction, workflow-phase checks, audit and checkpoints.
No new retry policy, scheduler, queue, lease or approval implementation.

#23 is not required. Normal integration may touch shared composition imports or
OpenAPI if #23 adds endpoints. If #23 also adds a migration, maintain a single
Alembic head when integrating the branches; no such integration was attempted here.

The only migration adds `session_messages` after `c22a4b8f901d`. Existing sessions
lack message storage. Upgrade/re-upgrade, metadata agreement, preservation of
sessions and refusal to downgrade nonempty history were verified.

## Security and behavioral evidence

- Grounding and prompt-injection containment run before any ask SSE text.
- Exact citation provenance and refusal payloads survive persistence.
- History never enters the model as evidence or trusted instructions.
- Foreign sessions are denied to reviewer/admin as well as analyst.
- Competing retries yield one 202, one 409 and one durable retry audit.
- FAILED retry preserves job identity, idempotency key and checkpoints.
- Retry cannot execute a workflow in AWAITING_APPROVAL.
- Concurrent history writes commit complete ordered pairs; second-row failure
  rolls back the question too.
- #17's mandatory safety pipeline, #19's human decision and #18's finalizer
  remain authoritative and unchanged.
- Job SSE disconnection/replay/cancellation behavior remains #21's behavior.

## Exact final verification

Commands used the existing Python 3.11 virtual environment unless Linux is noted.

| Command / suite | Result |
| --- | --- |
| pytest tests/unit/test_api_contracts.py -q | 51 passed |
| Related unit files: test_job_controls.py, test_idempotency_and_recovery.py, test_grounded_qa.py, test_workflow_safety_order.py, test_workflow_approval_pause.py, test_job_stream.py | 124 passed |
| pytest tests/unit -q -rs --tb=short | 1100 passed, 1 skipped |
| pytest tests/architecture -q | 3 passed |
| Existing integration files: test_error_handling.py, test_ownership_api.py, test_rbac_api.py, test_auth_api.py, test_orm_models.py, test_clinical_workflow_pipeline.py | 216 passed |
| Linux: pytest tests/integration/test_api_history.py tests/integration/test_jobs_api.py tests/integration/test_job_streaming.py -q --tb=short -p no:cacheprovider | 54 passed, including all 8 new PostgreSQL/Redis tests |
| ruff check app tests | All checks passed |
| ruff format --check app tests scripts/export_openapi.py migrations/versions/d24a1b9c830f_session_messages.py | 305 files already formatted |
| mypy app | Success: no issues found in 197 source files |
| Mypy including new export script and test/support files | Success: no issues found in 201 source files |
| lint-imports | 3 contracts kept, 0 broken |
| python -m scripts.export_openapi --check | Passed on Windows and Linux |
| git diff --check | Passed |

The single unit skip is the existing document permission case:
`tests/unit/domain/test_permissions.py:180`, because documents have no baseline
view permission. No #24 test was skipped.

A native Windows run of the combined job integration suite produced 31 passes
and 23 fixture errors in the existing SSE tests because psycopg rejects the
default Proactor loop. The unmodified suites then passed all 54 tests in Linux
using `domain-copilot-ticket12:local`, with only this worktree mounted read-only.
PostgreSQL and Redis were separate disposable #24 containers on ports 55424 and
56424. No shared application database or queue was used.

Observed warnings concern the existing Starlette/httpx deprecation, pytest-asyncio's
Windows loop-policy fixture compatibility, and Alembic's existing computed FTS
default. They did not fail the final checks.

## Remaining integration considerations

Apply `alembic upgrade head` before using persistent history. Framework 422
responses intentionally change to the common typed envelope. Ask SSE buffers
grounding before emitting source-text deltas and offers no durable replay;
job SSE retains durable replay. Repeating a synchronous ask records another
exchange; no new ask-idempotency guarantee is claimed. The existing session
detail URL retains its ownership projection, with metadata in session list/create
and full history at /messages. Offset lists are live views, not snapshots.

Ticket #27's broader contract suite remains out of scope. No unresolved #24
acceptance criterion remains.
