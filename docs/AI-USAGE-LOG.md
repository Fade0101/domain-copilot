# AI Usage Log

> What was delegated, what was written by hand, where the AI misled, and how it
> was verified. Maintained per BRD ENG-8.

---

## Log Format

Each entry records:
- **Ticket** — which ticket the work belongs to
- **Task** — what was delegated or written
- **AI role** — what the AI did (drafted, reviewed, generated, suggested)
- **Human role** — what the human did (designed, reviewed, accepted, corrected)
- **AI mistake** — where the AI was wrong (if applicable)
- **Verification** — how correctness was confirmed

---

## Entries

### Entry 1 — Project Foundation (Ticket #1)

| Field | Detail |
|-------|--------|
| **Ticket** | #1 — Establish Project Foundation & CI |
| **Task** | CI pipeline, pyproject.toml, ruff/mypy/import-linter config |
| **AI role** | Drafted the CI workflow (`.github/workflows/ci.yml`), `pyproject.toml` tool config, and `requirements.txt`. |
| **Human role** | Defined the CI stages required (lint, type-check, architecture contracts, test, audit, secret scan). Reviewed and approved the generated configs. Chose Python 3.11 and the specific tool versions. |
| **AI mistake** | None significant — the generated configs were straightforward and correct. |
| **Verification** | CI pipeline ran green on the first PR. All tools (`ruff`, `mypy`, `lint-imports`, `pytest`, `pip-audit`, `gitleaks`) executed without errors. |

---

### Entry 2 — Clean Architecture Foundation (Ticket #2)

| Field | Detail |
|-------|--------|
| **Ticket** | #2 — Establish Clean Hexagonal Architecture |
| **Task** | Domain entities, value objects, application ports, use cases, infrastructure adapters, presentation routes, composition root |
| **AI role** | Drafted entity classes, port protocols, the `RegisterDocument` use case, the in-memory repository adapter, FastAPI routes, and the composition root. Also generated the import-linter contracts and AST boundary tests. |
| **Human role** | Designed the layer dependency rules and the entity model (`Document`, `DocumentMetadata`, `DocumentStatus`). Decided which invariants to enforce in the domain layer vs. at the boundary. Reviewed all AI output for architecture violations. |
| **AI mistake** | The AI initially suggested importing `InMemoryDocumentRepository` directly from a route handler (`documents.py`) instead of going through the composition root. This would have created a direct `presentation → infrastructure` dependency, violating the hexagonal architecture contract. |
| **Verification** | The import-linter contracts in `pyproject.toml` caught the violation at lint time. The AI was corrected and the route was refactored to use `Depends()` → `container` → adapter. The human added the `.agents/rules/architecture.md` instruction file to prevent recurrence. Both `lint-imports` and `tests/architecture/test_boundaries.py` pass. |

---

### Entry 3 — Configuration, DI & Error Model (Ticket #3)

| Field | Detail |
|-------|--------|
| **Ticket** | #3 — Configuration, Dependency Injection & Error Model |
| **Task** | Nested pydantic-settings, prompt port + YAML adapter, typed error taxonomy, boundary error mapping |
| **AI role** | Drafted the nested settings models (`LLMSettings`, `EmbeddingSettings`, etc.), the `IPromptProvider` protocol and `Prompt` dataclass, the `YamlPromptProvider` adapter, the error taxonomy extensions (`ConfigurationError`, `PromptNotFoundError`, `PromptValidationError`, `JobNotFoundError`), and the error handler module (`app/presentation/api/errors.py`). Also drafted ADR-006 and updated ARCHITECTURE.md. |
| **Human role** | Designed the error taxonomy (which errors are client faults vs. server faults). Decided that `ConfigurationError` is a server fault mapped to 500 with a static message. Decided that the prompt version field inside the YAML is authoritative (not the filename). Reviewed all code for security implications. |
| **AI mistake — Error detail leakage** | The AI's first implementation of exception handlers returned `str(exc)` for **all** exceptions including `ConfigurationError`. This would have leaked internal file paths, prompt names, and configuration details to API callers. For example, a `PromptValidationError("prompt grounded_answer.v1.yaml missing keys: ['template']")` would have been returned verbatim as HTTP 500 response body. |
| **How caught** | The security reviewer sub-agent was invoked to review the error handling code. It flagged the 500 responses as violating SDD §A.5.1 ("never expose internal details"). |
| **Fix** | Server fault handlers (`ConfigurationError`, catch-all `Exception`) now return static messages (`"Application configuration error"`, `"Internal server error"`) and log the real cause server-side. Client fault handlers (422, 409, 404, 400) continue to return contextual messages since those are safe and useful. |
| **Verification** | Integration tests in `tests/integration/test_error_handling.py` verify that: (1) client faults return the error's own message, (2) server faults return a generic message, and (3) the response body includes a stable `code` field. The full test suite passes. |

---

### Entry 4 — Over-Eager Status Claims

| Field | Detail |
|-------|--------|
| **Ticket** | #3 (documentation update) |
| **Task** | Updating the BRD traceability matrix |
| **AI role** | The doc-writer sub-agent updated the BRD traceability matrix to mark AR-4 (Configuration & Prompts) as "✅ Implemented" after only the configuration surface was built, before any prompt infrastructure existed. |
| **Human role** | Caught the premature status during PR review. AR-4 requires both configuration AND prompts. At the time, only nested settings were implemented — no `IPromptProvider`, no `YamlPromptProvider`, no prompt YAML files. |
| **AI mistake** | Marking a requirement as fully implemented when only half of its scope was done. This is exactly the kind of false claim the assessment warns against: "claiming an unimplemented feature is worse than omitting it." |
| **Fix** | The status was reverted to "🔶 Partial" until both the configuration surface and the prompt infrastructure were complete. The doc-writer's SKILL.md was updated to explicitly forbid claiming unimplemented features. |
| **Verification** | Manual review of the BRD matrix against the actual codebase. AR-4 was only marked "✅ Implemented" in the final commit after both config and prompts were verified working with tests. |

---

### Entry 5 — Boolean Version Edge Case

| Field | Detail |
|-------|--------|
| **Ticket** | #3 (prompt validation) |
| **Task** | YAML prompt schema validation |
| **AI role** | The test writer generated tests for the `YamlPromptProvider` including tests for invalid version types (string, float, None). |
| **Human role** | Noticed a missing edge case: in Python, `bool` is a subclass of `int`, so `isinstance(True, int)` returns `True`. A YAML file with `version: true` would pass the integer check and be treated as version 1. |
| **AI mistake** | The AI's initial version validation was `isinstance(version, int)`, which accepts booleans. The test suite did not include a test for boolean versions. |
| **Fix** | Added `or isinstance(version, bool)` guard in `YamlPromptProvider._parse()` and a corresponding test case `test_rejects_boolean_version`. |
| **Verification** | The test `test_rejects_boolean_version` confirms that `version: true` raises `PromptValidationError`. The full test suite passes, including this edge case. |

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| Tickets covered | #1, #2, #3, #4 |
| AI mistakes caught | 4 (architecture violation, error leakage, false status claim, boolean edge case) |
| Mistakes caught by sub-agents | 1 (security reviewer → error leakage) |
| Mistakes caught by human review | 3 (architecture violation, false claim, boolean edge case) |
| Mistakes caught by automated tooling | 1 (import-linter → architecture violation) |

---

## Lessons Learned

1. **AI agents need constraints.** Without explicit instruction files, the AI
   optimises for the shortest path, not the architecturally correct path. The
   `.agents/rules/` files transformed AI suggestions from "sometimes violating
   boundaries" to "consistently respecting them."

2. **Security review is non-negotiable.** The error leakage issue would have
   shipped without the security reviewer sub-agent. Manual review caught the
   other issues, but having a specialised security perspective as a separate
   step adds genuine value.

3. **AI is bad at knowing what it doesn't know.** The false status claim and
   the boolean edge case both stem from the AI being confidently wrong. The
   human must verify any claim the AI makes about completeness or correctness.

4. **Automated gates complement human review.** The import-linter caught the
   architecture violation mechanically. The pre-commit hook ensures these checks
   run even when the human forgets. Neither replaces the other — both are needed.
