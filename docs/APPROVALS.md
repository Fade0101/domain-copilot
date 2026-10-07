# Human Approval Gate (#19)

The gate stores and reviews the actual Documentation Drafter output. Only a
persisted `APPROVED` decision can be passed to #18's guarded finalizer. Approval
does not call that tool or start an agent/worker; #17 owns execution after review.

## Registering a persisted review

#16 returns `ClinicalNoteDraft`; it does not persist it. Trusted application code
can now call:

```python
service = container.approval_service()
review = await service.prepare_review(DraftReview(draft, safety_verdict), job_id, principal)
```

`DraftReview` comes from `app.application.approvals.contracts`. It contains the
complete #16 draft and its authoritative #15 `SafetyVerdict`: note/digest,
asserted/excluded/deferred claims, citations, flags, reasons, refusal and
`can_proceed` values, evidence trace IDs, quarantine counts and metadata. Claims
must retain their safety provenance and both outputs must name the same workflow.
There is no HTTP endpoint accepting a draft or safety verdict for registration.

The workflow and job must already exist, have the same persisted owner, and be
`AWAITING_APPROVAL` and `STARTED`, respectively. A cancellation request prevents
registration. #19 writes `WorkflowRunModel.review_snapshot` and
`approval_job_id`, then an `approval.review_requested` event, in one transaction.
The snapshot limit is 4 MiB; oversized input is refused without truncation. An
identical registration is idempotent. Changed text, provenance or job identity
cannot replace a registered review. One job can be bound to only one review.

There is no new workflow/job producer here. #17 will register its completed
outputs, checkpoint the durable workflow reference, then raise #20's `JobPaused`.
#19 does not claim that registering a snapshot releases a worker.

Pending review is represented by the persisted snapshot and workflow phase;
there is no fabricated reviewer on a pending approval row. A decision row is
created only when an authorized human acts. Legacy #18 approval rows remain
readable by its finalizer, but #19 cannot review them without a registered snapshot.

## Review API and authorization

The existing API prefix is `/api/v1`. All routes authenticate using #5's JWT
resolver and reload the stored user. The service checks permissions even when
called without HTTP; the persistence transaction checks them again while locking
the actor's stored role.

| Method/path | Command body | Required permissions |
| --- | --- | --- |
| `GET /runs/{workflow_id}/approval` | None | `VIEW_PENDING_APPROVALS` |
| `POST /runs/{workflow_id}/approval/approve` | `{"draft_id": "<original SHA-256>"}` | `APPROVE_CLINICAL_NOTE` |
| `POST /runs/{workflow_id}/approval/reject` | `{"draft_id": "<original SHA-256>", "reason": "Additional evidence is needed."}` | `REJECT_CLINICAL_NOTE` |
| `POST /runs/{workflow_id}/approval/edit-and-approve` | `{"draft_id": "<original SHA-256>", "edited_note": "Clinician-reviewed content"}` | `EDIT_CLINICAL_NOTE` and `APPROVE_CLINICAL_NOTE` |

The existing permission matrix grants these review capabilities to reviewer and
admin, never analyst. Existing run ownership checks also apply. This repository
has no tenant model: reviewers/admins explicitly have `VIEW_ALL_RUNS` across
owners. #19 preserves that grant and does not invent a tenant or reviewer
assignment policy. A reviewer cannot supply a different job ID to complete an
unrelated job; the job is resolved from the immutable server-side run binding.

Review responses include `draft`, `safety_verdict`, workflow/job states,
`approval_status`, `approval_allowed`, cancellation state, the historical
`decision`, and any durable `finalization_request`. They use `Cache-Control:
no-store`. The note is never rebuilt from the case summary, transient agent state
or client content. Extra request fields (including role, actor, approval flags,
job IDs, original notes or citations) are rejected.

Refused, unsupported, flagged, incomplete or partial drafts can be viewed and
rejected, but not approved or edit-and-approved. Both `refused` and `can_proceed`
are checked: #16 can retain `can_proceed=True` on a capacity refusal. Approval
also requires complete cited SAFE claims. The gate does not change any agent
verdict or re-evaluate edited clinical content.

Rejection requires 3–4,000 characters after checking non-whitespace content and
at least one alphanumeric character. Edited notes must be nonempty, fit #18's
64,000-character limit, and differ from the original. Approve copies the exact
persisted original; edit-and-approve accepts only a replacement note body.
Structured safety/citation/provenance fields remain unchanged and visible beside
the human-authored version. New edited text is not labelled as newly verified
agent evidence.

Missing credentials return 401, denied permissions 403, unknown runs 404, stale
drafts/invalid states/conflicting decisions 409, invalid commands 422, and
unavailable approval storage a static 503. IDs and role-like headers cannot
override these checks.

## Decisions, diff and audit

`ApprovalModel` remains the decision authority consumed by #18. Its original
columns are retained; #19 adds `action`, `reviewer_role`, `draft_id`,
`approved_draft_id` and `diff`. `reviewer_id` is the actual actor, and `timestamp`
is the decision time. Original and approved note content are separate fields.

The server generates a deterministic unified textual diff with `original` and
`approved` labels, additions/removals/context, and missing-final-newline markers.
Line-ending changes remain visible. The diff is stored with the decision and
in its audit event, and is reproducible from the two persisted note versions.
An unedited approval has an empty diff and equal original/approved digests.

Decision audit uses the existing `AuditEntry` envelope in the existing
`job_events` table, with event type `approval.decision_recorded`. Its action
distinguishes `approval.approve`, `approval.reject`, and
`approval.edit_and_approve`. Actor, stored role, run/job/draft/approval identifiers,
time, reason and diff are durable in the same transaction as the decision. The
existing `IAuditSink` also receives post-commit identifier-only logging; clinical
text, reasons and diffs are not copied into that ordinary log.

PostgreSQL triggers prevent updates/deletes of #19 decisions, registered review
snapshots/bindings/owners, and #19 audit/signal events. A partial unique index
allows at most one #19 decision per workflow without invalidating legacy rows.
The same actor's exact repeated command returns the original decision with
`replayed: true` and no duplicate durable events. Another action, actor, edit or
reason receives 409. Replays still undergo fresh permission checks.

## Transactions and lifecycle

```text
Authenticated reviewer/admin
  -> load the persisted review
  -> validate action, draft digest and workflow/job state
  -> persist decision and audit
  -> persist resulting state and any finalization request
  -> commit
  -> return the durable result to the future #17 caller
```

The decision transaction uses the same PostgreSQL advisory-lock key as #20's
worker, with a transaction-scoped lock on the connection performing the writes.
An executing worker causes a 409 rather than a decision racing an active stage.
Workflow, approval and job rows are locked, and the actor's role is held stable
through commit. An audit or commit failure rolls back the entire operation.

| Decision | Workflow state | T7 job state | Handoff |
| --- | --- | --- | --- |
| Awaiting a human | `AWAITING_APPROVAL` | `STARTED` | No finalization request |
| Approve / edit-and-approve | `APPROVED` | `STARTED` | Durable finalization request |
| Reject | `REJECTED` | `COMPLETED` | No finalization request |

`APPROVED` is the existing schema's ready-for-finalization marker. #17 will add
its pipeline states; #19 does not add `RESEARCH`, `FINALIZE` or other execution
states. `PENDING_APPROVAL` is only a BRD alias for `AWAITING_APPROVAL`, never a
second stored workflow phase. API `approval_status=PENDING` describes the absence
of a human decision, not a T7 lifecycle state.

Rejection is a successful terminal clinical outcome, so the linked job completes
using `Job.transition(COMPLETED)`, with no error. It is not an infrastructure
failure. Checkpoints, attempt count, input and correlation identity remain intact.
The real T7 lifecycle has no `PAUSED` state: #20's `JobPaused` returns the task
while its logical job remains `STARTED`.

#21 streams these committed changes through
`GET /api/v1/jobs/{job_id}/stream` as `job_progress`, with separate `state`,
`workflow_id` and `workflow_state` fields. Review registration and decisions
share their transaction with the progress event. Approval audit details and
finalization requests remain in their existing authorized review interfaces;
SSE does not expose their private payloads or signal execution. See
[JOB_STREAMING.md](./JOB_STREAMING.md) for replay and explicit cancellation.

## Contract consumed by #17

`ApprovalService.get_decision(workflow_id, principal)` returns the persisted
`ApprovalDecision`, or `None` while the registered review is awaiting a decision.
It includes actor/role, workflow and original draft identity, action/status,
original and approved text/digests, reason, diff and timestamp. Workflow owners
with `RUN_WORKFLOW` may use this internal lookup; foreign analysts are denied.

Successful approve/edit-and-approve also persists an
`approval.finalization_requested` job event. Its `FinalizationRequest` contains
`event_id`, `workflow_id`, `job_id`, `approval_id`, `draft_id` and `created_at`.
It is returned in `DecisionResult.review.finalization_request` after commit and
can be read again with `get_finalization_request(workflow_id, principal)` after
a process restart. Pending and rejected reviews return no such signal. A separate
connection cannot observe a request whose decision/audit transaction has not
committed.

This is a durable handoff, not a running dispatcher. #17 can consume it, verify
its checkpoint and limits, and use #20's `JobService.resume(job_id)` for the
existing logical job. Broker delivery/reconciliation and failure policy remain
#17/#22 responsibilities; #19 introduces no scheduler, acknowledgement loop,
SSE transport or direct finalizer call.

When executing the handoff, #17 must establish its finalization phase and check
completed safety/draft outputs, refusal/provenance and execution limits. It must
then use the server-bound orchestrator tool with the persisted workflow, draft
and approval IDs. #18 still locks/rechecks approval, reviewer permissions and
ownership, copies only persisted reviewed text, and protects immutable replay.
The event and response are not replacements for that finalization guard.

## Migrations and verification

Apply `alembic upgrade head`. Revision `f19b6a2d9041` follows #18 and extends only
existing tables. It does not change the workflow or job state enums. Downgrade
is supported before #19 data exists and explicitly refuses to erase registered
reviews, decisions or audit afterward.

```text
python -m pytest tests/unit/application/test_approvals.py tests/unit/infrastructure/test_approval_snapshot.py -q
python -m pytest tests/integration/test_approvals_api.py -q
```

The integration suite requires `TEST_DATABASE_URL` for a disposable PostgreSQL
service with pgvector. It creates/drops its own database, migrates and exercises
real JWT resolution, SQL roles/ownership, transactions, advisory locks, immutable
audit, commit visibility and the production #18 finalizer. CI must configure the
service; local absence is an explicit skip, never a fake approval store.
