# Architecture Decision Records

This directory records significant architecture decisions for Domain Copilot.
The authoritative architecture baseline remains
[`SYSTEM-DESIGN.md`](../SYSTEM-DESIGN.md); ADRs capture *decisions* (context,
choice, consequences) as they are made per BRD **AR-7** (≥4 ADRs).

## Format

Each ADR uses: **Status** · **Context** · **Decision** · **Consequences**
(and, where relevant, **Enforcement**). Statuses: `Proposed`, `Accepted`,
`Superseded`, `Deprecated`.

## Index

| ADR | Title | Status | Ticket |
| --- | ----- | ------ | ------ |
| [ADR-001](./ADR-001-chunking-and-ingestion-embeddings.md) | Structure-aware chunking and ingestion embeddings | Accepted | #8 |
| [ADR-003](./ADR-003-vector-store-and-keyword-index.md) | Vector store & keyword index (pgvector, HNSW, FTS) | Accepted | #9 |
| [ADR-004](./ADR-004-async-job-execution.md) | Durable async jobs, Celery/Redis and checkpoint resume | Accepted | #20 |
| [ADR-005](./ADR-005-clean-hexagonal-boundary-enforcement.md) | Clean/Hexagonal boundary enforcement | Accepted | #2 |
| [ADR-006](./ADR-006-configuration-prompts-and-error-model.md) | Configuration, prompts & error model | Accepted | #3 |
| [ADR-007](./ADR-007-provider-abstraction-and-selection.md) | Provider abstraction & selection | Accepted | #7 |
| [ADR-008](./ADR-008-hybrid-retrieval-and-grounded-qa.md) | Hybrid retrieval, cross-encoder scores and grounded Q&A | Accepted | #10 |
| [ADR-009](./ADR-009-pii-phi-handling-and-redaction.md) | Healthcare PII/PHI Handling, Evaluation, and Redaction Decision | Accepted | #26 |

## Reserved (not yet written)

ADR-002 is **reserved** for the orchestration decision called out by AR-7 and
will be written by the ticket that implements it. ADR-001 records ingestion
chunking and embeddings; ADR-008 records the Ticket #10 fusion, reranking and
grounded-answer strategy.

| ADR | Reserved topic |
| --- | -------------- |
| ADR-002 | Multi-agent orchestration & approval gate |

Creating a reserved ADR prematurely (before its decision is made) would record a
speculative rather than a real decision, so each is deferred to its own ticket.
