# Ticket 20 — Async Job Queue

Base: initially `origin/dev` at `ae0fea0`; updated to `e1b0ff5` after Ticket #5
merged. Dependencies #1, #3 and #6 are present. All Ticket #20 changes are in
the isolated `feat/ticket-20-async-jobs` worktree; other checkouts are untouched.

1. Define the exact lifecycle and SDK-free job-store, queue, and handler ports.
2. Add PostgreSQL-first submission, polling, explicit dispatch reconciliation,
   and a generic runner with durable named-step checkpoints and exclusive execution.
3. Extend the existing jobs schema through one Alembic revision. Store inputs,
   results, safe errors, timestamps, cancellation flag, and checkpoints in PostgreSQL.
4. Implement PostgreSQL storage and Celery/Redis transport. Messages carry only job
   IDs. Provide a harmless diagnostic handler and a worker entrypoint.
5. Integrate HTTP 202 submission and polling with the merged Ticket #5 JWT and
   ownership checks. No separate operator-token authentication mechanism.
6. Test lifecycle and handler contracts, real Redis/PostgreSQL delivery, worker
   restart, broker loss, and reconciliation. Run migrations and all quality gates.
7. Document setup, handler integration, recovery boundaries, ADR-004, and actual
   evidence. Commit only Ticket 20 changes.

Out of scope: streaming/cancel transport, canonical-input idempotency policy,
automatic recovery scheduling, and ingestion/evaluation/clinical handlers.
Completed checkpoints are skipped; effects before a checkpoint commit remain
at least once and must be idempotent in the owning handler.

Implementation and verification completed on 2026-10-01:

- Exact lifecycle, PG authority, Celery/Redis, 202 submission, safe polling,
  named checkpoints, explicit resume/reconciliation, and handler ports implemented.
- Migration follows Ticket #5 with one head; test schemas own their version table.
- Full suite: 720 passed, one intentional permission-matrix skip.
- Real Linux image: 20 queue/API integration tests passed, including hard worker
  kill/resume and unchanged committed effect counts.
- Ruff lint/format, mypy, three import-linter contracts, Docker build and Compose
  validation passed. Dependency audit reported no known vulnerabilities.
- Gitleaks found no leaks in the staged Ticket #20 changes.
- ADR-004, setup/handler/recovery guide, architecture map and scoped BRD evidence
  updated; domain handlers and #21/#22 policies remain out of scope.

Commit as two logical changes: queue/store/runner and its tests/CI; then the
authenticated API, worker packaging and documentation.
