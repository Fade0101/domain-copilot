# ADR-004: Durable Async Job Execution

- **Status:** Accepted
- **Date:** 2026-10-01
- **Ticket:** #20 — Async Job Queue (Celery + Redis)
- **Requirements:** BRD AR-1, AR-3, AR-6, T7-01/02/04/05/09;
  SYSTEM-DESIGN §A.4.5 and §A.4.3. Ticket #5 supplies JWT authentication and
  persisted ownership. Tickets #8, #12 and #17 consume this runner.

## Context

Long jobs must run outside the API process, survive lost broker data, and resume
from committed checkpoints. Celery delivery and a Redis result backend alone do
not establish durable ownership, lifecycle state or results. The clinical
workflow can also intentionally wait for approval while its T7 job stays STARTED,
so a scan of STARTED jobs cannot safely treat every row as a crashed worker.

This decision fills the ADR number reserved for T7. The frozen SDD remains
unchanged. Ticket #20 delivers execution primitives; streaming/cancel transport
and automatic recovery/idempotency policy remain in #21 and #22 respectively.

## Decision

1. **PostgreSQL is authoritative.** Extend the existing owned `jobs` table with
   operation type, JSON input/result, correlation ID, execution timestamps and a
   cancellation flag. Checkpoints and safe errors stay in the same durable row.
   The additive migration follows Ticket #5's migration. Legacy rows remain
   readable and are excluded from dispatch until they have an operation type.

2. **Celery uses Redis for delivery only.** The JSON task contains a UUID, never
   job input, credentials, checkpoints or results. The Celery result backend is
   disabled, including when a process inherits `CELERY_RESULT_BACKEND`. The BRD's
   optional Redis result transport adds no capability needed here; polling reads
   PostgreSQL. Late acknowledgment, rejection on worker loss and prefetch 1 are
   enabled. They complement PostgreSQL recovery; they do not replace it.

3. **Persist before publishing.** Submission validates a registered operation,
   commits PENDING, commits QUEUED, and then publishes the UUID. Publishing has a
   bounded connection timeout and no automatic publish retry. A broker outage
   still returns an accepted durable job. PostgreSQL failures produce a safe
   server error. The creation/dispatch gap is closed by explicit reconciliation.

4. **Enforce the exact lifecycle.** Domain transition validation and row-locked
   persistence permit only `PENDING → QUEUED → STARTED → COMPLETED | FAILED |
   CANCELLED`. Each transition records its timestamps. Terminal redelivery does
   no work. Resuming an interrupted STARTED job preserves its state and attempt
   number. The FAILED-to-QUEUED retry exception in the full BRD belongs to #22
   and is deliberately unavailable in this ticket.

5. **Checkpoint named steps under exclusive execution.** `IJobHandler` and
   `IJobContext` are SDK-free application ports. `context.step(name, action)`
   returns a saved result or executes the action and commits its result before
   advancing. A PostgreSQL session advisory lock excludes concurrent execution
   of the same job and spans checkpoint transactions. The lock-holding connection
   performs that execution's reads/writes; an invalidated connection cannot be
   silently replaced while retaining a claim to the lock.

6. **Reuse authentication and ownership.** `POST /api/v1/jobs` requires Ticket
   #5's `MANAGE_ALL_JOBS` permission. The owner is derived from the resolved
   principal. `GET /api/v1/jobs/{job_id}` uses the existing ownership service:
   owners can poll; cross-user access requires the admin grant. The generic
   operational endpoint cannot substitute for the domain permission and approval
   checks that feature routes/handlers must enforce.

7. **Make recovery explicit.** `reconcile` republishes PENDING/QUEUED rows in
   bounded batches. `resume JOB_ID` republishes an operator-confirmed interrupted
   STARTED job. `JobPaused` releases the worker while leaving STARTED, and
   automatic reconciliation excludes that state. Heartbeats, phase-aware
   scheduling, backoff, request idempotency and retry policy remain #22.

## Consequences

- Losing Redis does not lose committed jobs, results or checkpoints. Duplicate
  messages are harmless while another execution owns the lock or after a job is
  terminal. Delivery remains at least once.
- Checkpoint completion is the resume boundary. A crash after an external effect
  but before its checkpoint commits can repeat that effect. Handlers must make
  such effects idempotent or transactional; this is not an exactly-once claim.
- Each running job holds one PostgreSQL session. Direct connections or session
  pooling are required; a transaction-pooling proxy cannot preserve this lock.
  Steps must be awaited sequentially. Connection loss stops durable progress,
  but cannot undo external work already performed by a handler.
- New handlers are registered in the composition root, with the same registry
  deployed to API and worker. Only the harmless `diagnostic` handler ships here.
  Owner and correlation IDs are available to handlers; current authorization
  must be resolved before sensitive effects rather than inferred from queued data.
- PostgreSQL/Redis integration tests launch real separate Celery processes,
  kill a worker after a committed checkpoint, resume it, and assert each completed
  step's effect occurs once. CI supplies real services and refuses silent skips
  when their test configuration is missing.

Setup, API examples and operational boundaries are in [JOBS.md](../JOBS.md).
