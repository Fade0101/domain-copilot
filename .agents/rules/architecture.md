# Architecture Rules — AI Development Guardrails

These rules **must** be followed by all AI agents working in this repository.
They encode the Clean Hexagonal Architecture decisions from ADR-005 and ADR-006.

## Layer Dependency Direction

Dependencies flow **inward only**: presentation → core → application → domain.

- **`app/domain/`** — Pure business rules. NO imports from `app.application`,
  `app.infrastructure`, `app.presentation`, `app.core`, or any third-party
  library (no `pydantic`, `fastapi`, `yaml`, `sqlalchemy`, `celery`, etc.).
- **`app/application/`** — Use cases and ports. Imports ONLY from `app.domain`.
  Same third-party restrictions as domain.
- **`app/infrastructure/`** — Adapters implementing application ports. SDK/ORM/
  queue libraries live here and NOWHERE else.
- **`app/presentation/`** — FastAPI routes, schemas, SSE. Never instantiates an
  adapter directly — always goes through `app.core.container`.
- **`app/core/`** — Configuration (`config.py`) and the composition root
  (`container.py`). The ONLY place that wires adapters to ports.

## Forbidden Patterns

1. **Never import infrastructure from presentation directly.** Use `Depends()`
   bridging through `app.presentation.api.dependencies` → `app.core.container`.
2. **Never raise `HTTPException` from a use case.** Use typed domain/application
   errors; the boundary mapper in `app/presentation/api/errors.py` translates them.
3. **Never put inline prompt strings in application or domain code.** Prompts are
   versioned YAML artifacts loaded via `IPromptProvider`.
4. **Never commit secrets.** API keys use `SecretStr` and are read from environment
   variables. `.env` is gitignored; `.env.example` has blank values.
5. **Never add `pydantic` or `pydantic_settings` to domain or application layers.**
   Pydantic is an edge concern restricted to `app/core/config.py` and
   `app/presentation/api/schemas/`.

## Commit Conventions

- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`.
- Atomic commits — one logical change per commit, no multi-thousand-line dumps.
- Every commit message explains **why**, not just what.

## Testing Conventions

- Domain and application tests use **fakes/stubs**, never real infrastructure.
- Integration tests go through the FastAPI `TestClient`.
- Architecture boundary tests in `tests/architecture/` must pass alongside
  `import-linter` contracts in `pyproject.toml`.

## File Placement

| Kind of code | Target path |
| --- | --- |
| Entity, value object, domain error | `app/domain/<context>/` |
| Use case, port, command, DTO | `app/application/<context>/` |
| Adapter (DB, LLM, queue, vector store) | `app/infrastructure/<concern>/` |
| Route, schema, middleware | `app/presentation/api/` |
| Configuration, composition root | `app/core/` |
| ADR | `docs/adr/` |
| Prompt artifact | `prompts/` |
