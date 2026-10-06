# Six clinical tools (Ticket #18)

The tool layer has exactly six agent-callable names. It supplies typed contracts,
restricted execution, evidence access, and an approval-gated final-note write.
It does not implement agents, orchestration, or the human approval workflow.

| Tool | Server-bound owner | Behavior |
| --- | --- | --- |
| `search_corpus` | Guideline Researcher | Existing #10 dense + PostgreSQL FTS + RRF (default k=60) + reranking. Returns indexed citations. |
| `retrieve_drug_info` | Guideline Researcher | Targeted drug/topic query through the existing grounded `AskUseCase`. |
| `check_interactions` | Safety Checker | Retrieves explicit interaction evidence for drugs/conditions through `AskUseCase`. |
| `validate_dosage` | Safety Checker | Verifies a complete dosage claim against explicit, verbatim evidence selected by `AskUseCase`. |
| `draft_clinical_note` | Documentation Drafter | Assembles a structured JSON draft from grounded evidence and separately labelled, unverified case context. No note or approval is saved. |
| `finalize_clinical_note` | Orchestrator | Independently verifies a persisted approval in PostgreSQL before writing the final note. |

There is no registration API, plugin discovery, configurable tool list, or seventh
clinical tool. Traces, audit records and persistence adapters are internal services.

## Contracts and evidence semantics

The input/output dataclasses are in
`app/application/clinical_tools/contracts.py`. Their constructors enforce value
invariants even for in-process callers. `ClinicalToolContracts` exports strict
JSON schemas and validates both directions, using Pydantic only in infrastructure.
The existing #7 `ToolDefinition`, `ToolCall`, and `ToolResult` are reused.

| Tool | Input fields | Principal output fields |
| --- | --- | --- |
| `search_corpus` | `query` | `citations`, `refused`, `evidence_trace_id` |
| `retrieve_drug_info` | `drug`, `topic` (default `overview`; also `contraindications`, `interactions`, `dosage`) | drug/topic, `evidence`, citations, refusal, evidence trace |
| `check_interactions` | `drugs`, optional `conditions` | drugs/conditions, evidence, citations, refusal, evidence trace |
| `validate_dosage` | `drug`, `dosage_claim` | drug/claim, `status`, evidence, citations, refusal, evidence trace |
| `draft_clinical_note` | `clinical_question`, `case_summary` | `workflow_id`, `draft_id`, `note`, citations, refusal, evidence trace, `requires_review: true` |
| `finalize_clinical_note` | `workflow_id`, `draft_id`, `approval_id` | `note_id`, workflow/draft/approval IDs, `note`, `finalized_by`, `finalized_at`, `created` |

Arguments must be a JSON object of at most 32 KiB. Unknown fields, duplicate JSON
keys, non-finite numbers, inappropriate scalar coercion and invalid Unicode are
rejected. Queries are limited to 2,000 characters, case context to 4,000, drug and
condition names to 100 each, and dosage claims to 1,500. Interaction inputs allow
1–8 drugs and 0–5 conditions, with at least two total entries and no duplicates.

The four evidence tools use the actual #10 retrieval/grounding paths and existing
configuration. No parallel search engine, fusion method, prompt policy, or model
provider is introduced. Citations retain #10's exact seven fields; their
`relevance_score` is the normalized cross-encoder score, **not clinical certainty**.
See [RETRIEVAL.md](RETRIEVAL.md) for the underlying grounding limits.

Interaction results are source quotations, not a declaration that a combination
is safe. An absent interaction in a search is not evidence of no interaction.
Dosage `SUPPORTED_BY_CORPUS` means the requested **complete claim**, including the
drug, matches a whole selected chunk after whitespace/case normalization. It does
not mean a dose is clinically appropriate for an individual. There is no unit
conversion, extrapolation, substring matching, or removal of qualifiers/negations.
A claim that cannot be verified returns `NOT_VERIFIED` and the exact refusal:

> Not enough information in the corpus

Drafting uses no additional generative policy. It copies the grounded answer and
real citations into a deterministic JSON document. User-supplied case context is
separate and marked `unverified`; limitations and the need for clinician review
remain visible. The tool performs no clinical Safety Checker review. The future
agents and approval workflow own those decisions. Repeating the same draft inputs
and evidence yields the same draft content/hash; trace IDs are outside that hash.

## Trusted binding and authorization

`Container.clinical_tool_factory()` reuses the existing Database, user repository,
AuthorizationService, retrieval/ask use cases and audit observer. A configured
PostgreSQL database is required; there is no in-memory finalization fallback.

Only trusted application code retains the factory. Each future agent receives a
single executor created with a **literal server-selected factory method**, an
authenticated #5 `Principal`, and a persisted workflow ID:

```python
# principal has already been resolved through #5 authentication.
factory = container.clinical_tool_factory()
research_tools = factory.for_guideline_researcher(principal, workflow_id)
# Also available to trusted wiring: for_safety_checker,
# for_documentation_drafter, for_orchestrator.

# Pass research_tools.definitions() to the existing #7 CompletionRequest.tools.
# Feed a returned #7 ToolCall to this SAME bound executor:
result = await research_tools.execute(tool_call)
```

Never select the factory method from model output, HTTP JSON/headers, a user-claimed
agent name, or a prompt. No HTTP endpoint exposing such a selector is added.
An executor accepts no principal/role/agent argument. Python application code is
trusted; a model receives JSON tool definitions/results, not Python object access.

Every execution reloads the user from the #5 repository, so a deleted account or
changed role takes effect immediately. Every tool requires `RUN_WORKFLOW` and
the existing persisted workflow ownership check. The first four additionally
require `ASK_QUESTION`. These existing permissions are cumulative across analyst,
reviewer and admin; existing reviewer/admin cross-run access is preserved. No
human role can override the fixed agent allow-list. Read/draft executors are not
given the final-note writer at all.

Finalization can run on behalf of the analyst who owns the workflow **after a
reviewer approves it**. It is not itself an approval decision. Inside its write
transaction the finalizer reloads and locks the actor/reviewer records and checks
the existing reviewer's `APPROVE_CLINICAL_NOTE` permission, plus the executing
actor's `RUN_WORKFLOW` and persisted ownership access. An analyst cannot make
their own approval record count as an authorized reviewer decision.

## Independent approval verification

Ticket #6 already provides `ApprovalModel`: workflow ID, reviewer ID, original
draft, reviewed text, and `PENDING`/`APPROVED`/`REJECTED`. Those fields are reused
unchanged. The [#19 approval gate](./APPROVALS.md) preserves the exact returned
draft `note` in `original_note` and persists the clinician-reviewed text in
`approved_note`. The `draft_id` is SHA-256 of the exact original draft's UTF-8
bytes. It is a content/version identifier; the separately checked workflow and
approval IDs prevent a matching draft in another workflow from being used.

#19 registers the full #16/#15 review snapshot and stores immutable human
decisions, including actor role and edit diff. It exposes a committed finalization
request for #17 without calling this tool. Refused and partial drafts cannot
receive a #19 APPROVED decision. The independent checks below remain unchanged.

`PostgresFinalClinicalNoteWriter.finalize()` performs one transaction:

1. Lock the workflow row, then load and lock the requested approval.
2. Require the matching workflow/approval IDs and status **exactly `APPROVED`**.
3. Verify the original draft hash and require nonempty persisted reviewed text.
4. Reload/lock actor and reviewer records and enforce existing RBAC/ownership.
5. Only then insert the final note, copying `approved_note` from that locked row.

The tool accepts neither final-note content nor `approved=true`. A concurrent
approval edit/revocation is serialized with verification and the write; an
uncommitted or revoked decision cannot be read as authorization. The tool does
not create approvals, modify their status/text, or change workflow state.

The existing schema lacked a finalization record distinct from reviewed candidate
text. Additive Alembic revision `e18a9d70c342`, following `95c7e8a12d40`, introduces
only `final_clinical_notes`. Its immutable application record links the workflow,
approval, original draft hash, copied reviewed content, actor and timestamp. Unique
constraints enforce one final note per workflow/approval. Replays return the same
note (`created: false`) **after checking approval again**. A different candidate
cannot overwrite it. Later approval changes do not silently edit historical final
notes. No existing approval model or state machine is replaced.

Apply with `alembic upgrade head`. Downgrade removes the new final-note table and
must not be used as a data-preserving rollback once final notes have been stored.

## Results, failures and audit

The existing #7 `ToolResult.output` contains a JSON envelope:

```json
{"ok": true, "tool": "search_corpus", "trace_id": "...", "result": {"...": "typed output"}}
```

Failures use `ok: false` and a typed `error` with a stable `code` and static safe
`message`. Codes distinguish invalid arguments, missing identity, permission
denial, unknown tool, missing workflow, required/mismatched approval, an existing
conflicting final note, unavailable storage/provider, and invalid output. Raw
exceptions, input bodies, credentials and clinical-note text are not returned in
errors. Application/domain exception types are defined in `clinical_tools/errors.py`
and reuse #3/#5's existing error families, including `ApprovalRequiredError`.

All executions and denials use the existing `IAuditSink`/logging path and the #6
`TraceModel`/`SpanModel` through the existing PostgreSQL retrieval audit adapter.
Tool spans capture the server-bound agent, authoritative role, workflow, safe IDs,
outcome/error, duration, and links to the normal #10 evidence traces and scores.
Arguments and draft/final content are excluded from tool audit entries. A removed
user or unavailable trace database falls back to the existing audit logger; trace
persistence remains best effort, as in #10. Final-note provenance is durable in
the same transaction as the final content.

## Verification

```bash
python -m pytest tests/unit/application/test_clinical_tools.py
# Point TEST_DATABASE_URL at a disposable PostgreSQL service with pgvector:
python -m pytest tests/integration/test_clinical_tools.py
```

The integration suite creates a separate temporary database, migrates the real
schema, and uses the production container, signed tokens, SQL user/ownership
adapters, approval/final-note persistence and trace tables. Only embedding,
reranking and chat computation use deterministic test adapters; authorization and
approval are never mocked. It verifies missing/pending/rejected/mismatched
approvals, role demotion, foreign ownership, argument spoofing, real dense/keyword
retrieval and citation resolution, non-writing drafts, concurrent finalization,
revocation while a call waits on the approval lock, durable results and safe audit.
In CI, missing PostgreSQL is a failure for this critical suite, not a silent skip.
