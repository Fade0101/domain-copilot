# ADR-006: Configuration, Prompts & Error Model

- **Status:** Accepted
- **Date:** 2026-09-29
- **Ticket:** #3 (Configuration, Dependency Injection & Error Model)
- **Requirements:** BRD AR-3 (Dependency Injection), AR-4 (Configuration & Prompts), AR-5 (Domain Errors); SYSTEM-DESIGN §A.5.1 (error handler never exposes internal details), §A.5.2 (orchestration limits, retry/backoff); constraint C6 (no secrets in the repo)

## Context

ADR-005 established the layered skeleton, a composition root, a typed-error base,
and one vertical slice. Three architectural requirements were only partly met and
block every downstream ticket:

- **AR-3** — DI existed but wired only `RegisterDocument`.
- **AR-4** — configuration held three app-level fields; there were no prompt
  artifacts, no prompt loader, and no prompt port. "Prompts as versioned
  artifacts, never inline literals" was undocumented and unenforced.
- **AR-5** — a typed taxonomy existed, but the HTTP boundary emitted an ad-hoc
  `{"detail": str(exc)}` and had no fail-safe handler, so an unexpected exception
  would leak internal detail to the caller — contrary to §A.5.1.

This ticket completes the configuration surface, adds versioned prompts behind a
port, and makes the error model complete and safe at the boundary.

## Decision

1. **Externalised, grouped configuration via pydantic-settings** (`app/core/config.py`,
   the only place pydantic-settings lives). `Settings` composes nested `BaseModel`
   groups — `llm`, `embedding`, `queue` (T7), `retrieval`, `limits`, `retry`,
   `prompts` — populated with the `__` nested delimiter (`LLM__MODEL` →
   `settings.llm.model`). Orchestration limits (max iterations 10, per-step
   timeout 60s) and the retry/backoff policy (3 retries) come straight from
   §A.5.2. Every field has a safe default so the app boots without a `.env`.

2. **Secrets never live in code or git (C6).** API keys are typed
   `SecretStr | None = None`; the real value is supplied by the environment at
   runtime and `SecretStr` masks it in logs and `repr`. `.env.example` documents
   every variable with blank secret values.

3. **Prompts are versioned artifacts behind a port (AR-4).**
   - `app/application/ports/prompts.py` defines the framework-free `Prompt` value
     (`id`, `version`, `description`, `input_variables`, `template`) with a
     `render()` that validates variables, and the `IPromptProvider` Protocol. The
     application depends on this port, never on a YAML library.
   - `app/infrastructure/prompts/yaml_prompt_provider.py` is the only module that
     imports `yaml`. It loads `prompts/*.yaml`, validates the schema, and — when
     `strict` — does so eagerly at construction, so a malformed prompt fails app
     **startup**, not a later request.
   - Two real, minimal artifacts anchor the mechanism: `grounded_answer.v1.yaml`
     and `safety_check.v1.yaml`. They demonstrate versioning, input variables,
     evidence grounding, refusal behavior, and the healthcare safety boundary.
     They are **not** the full agent prompts — those belong to #14/#15/#16.

4. **Deterministic prompt versioning.** `version` is an integer (file convention
   `<id>.v<version>.yaml`, but the field inside the file is authoritative).
   `get(id)` returns the numerically highest version; `get(id, version=n)` returns
   exactly version `n`. Duplicate `(id, version)` across files is a load-time
   error.

5. **Complete typed-error model, mapped by fault (AR-5; §A.5.1).**
   - Application taxonomy gains `JobNotFoundError` (⊂ `ResourceNotFoundError`,
     AR-5-named for T7) and a `ConfigurationError` base with `PromptNotFoundError`
     / `PromptValidationError`. A missing/invalid prompt is a **server fault**,
     not client input.
   - `app/presentation/api/errors.py` is the single source of truth. It maps
     errors by type to a stable body `{"detail", "code"}`:

     | Error | Status | Code | Message source |
     | ----- | ------ | ---- | -------------- |
     | `InvariantViolationError` | 422 | `INVARIANT_VIOLATION` | `str(exc)` |
     | `InvalidStateTransitionError` | 409 | `INVALID_STATE_TRANSITION` | `str(exc)` |
     | `ResourceNotFoundError` | 404 | `RESOURCE_NOT_FOUND` | `str(exc)` |
     | `ConfigurationError` | 500 | `CONFIGURATION_ERROR` | static |
     | `DomainError` | 400 | `DOMAIN_ERROR` | `str(exc)` |
     | `ApplicationError` | 400 | `APPLICATION_ERROR` | `str(exc)` |
     | *(any) `Exception`* | 500 | `INTERNAL_ERROR` | static |

   - **Client faults** carry their own message (safe and useful). **Server
     faults** (configuration, unexpected) carry a fixed generic message; the real
     cause is logged server-side and never returned (§A.5.1). Starlette resolves
     handlers by MRO, so `ConfigurationError` wins over `ApplicationError`, and
     the `Exception` catch-all only runs for anything otherwise unmapped.

6. **DI conventions (AR-3).** The composition root (`app/core/container.py`) is
   the only adapter-construction site; it now also builds the prompt provider from
   config (so startup validates prompts). Services take dependencies by
   constructor injection; FastAPI `Depends()` bridges request scope to the
   container via `app/presentation/api/dependencies.py`. This is implemented for
   the current application surface; future adapters are wired here as their
   tickets land.

## Enforcement

| Mechanism | What it adds this ticket | Where it runs |
| --------- | ------------------------ | ------------- |
| **import-linter** | `yaml` added to the domain-pure and application forbidden lists — inner layers may not import a YAML parser. | CI step `lint-imports` |
| **AST boundary test** | `yaml` added to `_FORBIDDEN_THIRD_PARTY`. | Normal `pytest` step |
| **Error-handling integration test** | `tests/integration/test_error_handling.py` asserts each mapping and that 500 bodies never contain the raised message (no leak). | Normal `pytest` step |
| **Eager prompt validation** | The container loads + validates `prompts/*.yaml` at startup; a bad prompt fails boot. | App startup / integration tests |

## Consequences

**Positive**

- Providers, queue, retrieval, limits, and retries are all configurable from the
  environment with safe defaults; secrets stay out of the repo (C6).
- Prompts are versioned, validated, and swappable without code changes; the
  "never inline" rule is enforced by the boundary checks.
- Every error reaches the client as a stable `{detail, code}`; internal detail
  can no longer leak through the boundary (§A.5.1).

**Negative / accepted trade-offs**

- Config groups and prompt artifacts exist ahead of the adapters that consume
  them (#7/#9/#20) — accepted: the config surface is the point of this ticket, and
  stubbing it now gives downstream tickets stable anchors.
- A `code` field is added to error bodies; the existing `detail` field is retained
  for back-compat.

## Notes

Only ADR-006 is created by this ticket. ADR-001–004 remain reserved for the
decisions they name (see [`README.md`](./README.md)).
