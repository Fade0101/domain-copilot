# ADR-008: Hybrid retrieval and grounded Q&A

- **Status:** Accepted
- **Date:** 2026-10-03
- **Ticket:** #10
- **Requirements:** BRD AC-2.1–2.7; AR-1–4; SYSTEM-DESIGN §A.5.5

## Context

Ticket #9 already owns pgvector cosine search, PostgreSQL FTS, citation metadata
and embedding provenance. Ticket #7 owns query embeddings, chat completion and
provider fallback. Ticket #8 supplies structure-aware indexed chunks. Their
interfaces must remain authoritative while the application combines evidence
and answers questions without inventing clinical information.

## Decision

1. Fuse independent #9 rankings with RRF, `sum(1 / (k + rank))`, one-based ranks
   and **k=60** by default. A chunk contributes once per source. UUID tie breaks
   make equal-score ordering deterministic. Deployment configuration bounds
   candidate and context sizes and may tune k; clients cannot select a fusion
   algorithm or arbitrary branch weights.
2. Rerank fused candidates with **BAAI/bge-reranker-v2-m3**, revision
   `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, behind the new SDK-free `IReranker`
   port. A cross-encoder jointly attends to question and passage, improving
   precision after inexpensive candidate retrieval. It is local and requires no
   paid model API. Loading/inference are lazy and off the API event loop; model
   code from the remote repository is disabled.
3. Expose `sigmoid(logit)` as `relevance_score`, with a default minimum of 0.5.
   This is a relevance score, not a calibrated probability of clinical truth.
   Preserve raw dense, keyword and RRF scores in telemetry. Scoring failure is
   a service error; never silently answer without reranking.
4. Use #7 completion and `IPromptProvider` for grounded evidence selection.
   Grounded-answer v2 keeps the policy in a system message and question/evidence
   in untrusted user data. The model returns a verdict and existing chunk IDs.
   The application quotes complete indexed chunks and builds the exact seven
   citation fields from search results. It rejects unknown/duplicate IDs and
   undeclared output fields. This prevents generated dosage prose, fake page
   numbers or selective removal of a source qualifier from entering the answer.
5. Return exactly **Not enough information in the corpus** for missing,
   below-threshold, conflicting or invalidly grounded evidence. Explicit
   high-risk evidence checks and literal-conflict checks fail conservatively;
   the prompt also assesses contextual relevance/conflicts. These Q&A controls
   do not implement the clinical-note Safety Checker or guarantee source truth.
6. Reuse `IAuditSink`, the logging sink and #6's existing trace/span schema. Record
   query, ranks/scores, candidate counts, selected/cited IDs, latency and refusal
   with the authenticated actor and one trace UUID. Share the existing database
   pool; retain the audit event if trace persistence is unavailable. No migration,
   new tracing backend, job handler or transport is needed.

## Consequences

- Answers are extractive and can be longer or more conservative than free-form
  prose. They preserve the clinical meaning of the selected source text.
- Literal-conflict detection may refuse superficially conflicting evidence even
  when a clinician could reconcile different contexts. Semantic correctness is
  not inferred from the reranker score or from passing lexical checks.
- BGE adds approximately 2.27 GB of model weights and CPU/RAM latency. Warm caches,
  bounded batches and explicit timeouts are necessary. Cancellation releases the
  caller while an already-running model thread retains its capacity slot.
- Query text is present in audit records. The synthetic/non-PII policy and access
  restrictions apply; no PII redaction or new log-retention service is added.
- Golden sets/evaluation (#12), streaming (#21), agents (#17), Safety Checker
  (#15) and corpus assembly (#11) remain outside this change.

## Verification

- `test_hybrid_retrieval.py` verifies the RRF formula, k=60, ordering, confidence
  filtering, invalid scorer responses and trace contents.
- `test_grounded_qa.py` verifies normal/empty/low/conflicting evidence, unsupported
  dosage/contraindication/interaction questions, exact refusal and citation IDs.
- `test_reranking_provider.py` verifies the pinned model, explicit sigmoid,
  thread execution, cancellation capacity and safe failures.
- `test_knowledge_api.py` verifies real PostgreSQL/pgvector/FTS, citations, JWT
  enforcement and existing trace ownership. The opt-in real-model smoke verifies
  actual MiniLM/BGE inference. Existing ORM/migration tests check schema agreement.

Operational instructions and score semantics are in [RETRIEVAL.md](../RETRIEVAL.md).
