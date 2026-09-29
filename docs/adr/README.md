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
| [ADR-005](./ADR-005-clean-hexagonal-boundary-enforcement.md) | Clean/Hexagonal boundary enforcement | Accepted | #2 |
| [ADR-006](./ADR-006-configuration-prompts-and-error-model.md) | Configuration, prompts & error model | Accepted | #3 |

## Reserved (not yet written)

ADR-001–004 are **reserved** for the major decisions called out by AR-7 and are
written by the ticket that actually makes each decision — they are intentionally
*not* created here:

| ADR | Reserved topic |
| --- | -------------- |
| ADR-001 | Chunking & hybrid retrieval strategy |
| ADR-002 | Multi-agent orchestration & approval gate |
| ADR-003 | Vector store / persistence choice (pgvector) |
| ADR-004 | T7 async job execution (Celery/Redis, resumability) |

Creating a reserved ADR prematurely (before its decision is made) would record a
speculative rather than a real decision, so each is deferred to its own ticket.
