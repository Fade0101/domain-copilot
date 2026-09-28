# ADR-005: Clean/Hexagonal Boundary Enforcement

- **Status:** Accepted
- **Date:** 2026-09-28
- **Ticket:** #2 (Establish Project Architecture & Dependency Rules)
- **Requirements:** BRD AR-1 (Clean Architecture), AR-3 (Dependency Injection), AR-5 (Domain Errors); SYSTEM-DESIGN §A.3.2 (per-layer import bans)

## Context

The frozen design (`SYSTEM-DESIGN.md` SDD-DC-D0T7 v1.1, `BRD.md` v1.2) mandates
Clean/Hexagonal (Ports & Adapters) architecture: business and application logic
must be independent of FastAPI, the ORM, the vector store, and LLM SDKs, so that
swapping a provider is "config + one adapter" with no change to business logic.

Prior to this ticket `app/` was empty scaffolding — the rules existed only as
prose. Prose boundaries rot: as later tickets add ingestion, retrieval, agents,
and T7 jobs, nothing stops an SDK import leaking into the domain. We need the
boundaries expressed as **code that fails the build** when violated, plus a
concrete, runnable slice proving the layering works end to end.

## Decision

1. **Four application layers with strictly inward dependencies**, mapped to the
   actual package tree:
   - `app/domain` — entities, value objects, domain errors. Pure standard
     library; imports no other `app.*` layer and no third-party package.
   - `app/application` — use cases, ports (as `typing.Protocol`), command/DTO
     objects. Imports only `app.domain`; no framework, ORM, or SDK; no pydantic.
   - `app/infrastructure` — adapters implementing the ports (SDKs live here).
   - `app/presentation` — FastAPI routes, pydantic schemas, `Depends()` wiring.
   - `app/core` — configuration and the **composition root** (`container.py`).

2. **Ports are `typing.Protocol`s owned by the application layer.** Adapters in
   `app/infrastructure` explicitly subclass their Protocol (greppable + checked
   by mypy). The container annotates each adapter with its port type, so
   conformance is verified at the wiring site.

3. **Dependency inversion via a single composition root.** `app/core/container.py`
   is the *only* module that imports `app.infrastructure`. It constructs the
   concrete adapters and injects them (constructor injection) into use cases;
   FastAPI `Depends()` resolves use cases from the container. The presentation
   layer never instantiates an adapter directly (AR-3).

   > `core` importing `domain` + `application` + `infrastructure` is intentional
   > and correct — it is the wiring layer. Infrastructure must **not** be made to
   > depend on `core` to "fix" this direction.

4. **Domain failures are explicit typed errors** (AR-5): a `DomainError` base with
   `InvariantViolationError` (→ HTTP 422) and `InvalidStateTransitionError`
   (→ HTTP 409), plus seeded anchors (`InsufficientEvidenceError`,
   `SafetyCheckFailedError`, `ApprovalRequiredError`) for later workflows. The
   presentation layer maps these to HTTP status codes; inner layers never import
   `fastapi.HTTPException`.

5. **Representative slice = `RegisterDocument`.** `POST /api/v1/documents` runs
   HTTP → schema → command → `RegisterDocumentUseCase` → `IDocumentRepository`
   → `InMemoryDocumentRepository`, exercising three ports (repository, clock,
   id-generator). Registration is **idempotent by `content_hash`**: a new hash
   creates and persists a document (HTTP 201); an existing hash returns the
   existing document without creating a duplicate or overwriting its metadata
   (HTTP 200). The in-memory adapter is the interim persistence; a
   SQLAlchemy/pgvector adapter arrives in a later ticket behind the same port.

6. **Boundaries are enforced two complementary ways** (see Enforcement).

## Enforcement

| Mechanism | What it checks | Where it runs |
| --------- | -------------- | ------------- |
| **import-linter** contracts (`pyproject.toml [tool.importlinter]`) | Declarative `forbidden` contracts encoding §A.3.2: domain imports no layer/SDK; application imports only domain; presentation never imports infrastructure. | CI step `lint-imports` |
| **AST boundary test** (`tests/architecture/test_boundaries.py`) | Dependency-free scan asserting the same rules, human-readable for reviewers. | Normal `pytest` step |
| **Integration test** (`tests/integration/test_documents_api.py`) | The adapter resolves only via `Depends()` → container, proving presentation does not construct infrastructure. | Normal `pytest` step |

There is deliberately **no "explicitly justified exception"** escape hatch for
domain/application SDK imports — they are forbidden outright.

## Consequences

**Positive**

- Boundary violations fail CI immediately, so the architecture cannot silently erode.
- The application layer is testable with fakes alone — no framework, DB, or LLM (AR-8).
- Providers/persistence are swappable behind ports without touching business logic (AR-1).
- New contributors get a "where does new code go?" answer and a worked example.

**Negative / accepted trade-offs**

- Some indirection (ports + container wiring) for a currently small surface — accepted, because the downstream surface (providers, agents, jobs) is large.
- Two enforcement mechanisms overlap — accepted: import-linter is authoritative and declarative; the AST test is reviewer-facing and runs without extra tooling.

## Notes

Only ADR-005 is created by this ticket. ADR-001–004 remain reserved for the
decisions they name (see [`README.md`](./README.md)) and are written by their own
tickets.
