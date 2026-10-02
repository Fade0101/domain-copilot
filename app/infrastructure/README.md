# Infrastructure layer

Concrete **adapters** that implement the ports declared in
`app/application/ports/`. This is the only layer (besides `app/core`, the
composition root) allowed to import third-party SDKs and drivers — FastAPI does
not belong here, ORMs/vector clients/LLM SDKs do.

Dependency rules (enforced by import-linter + `tests/architecture/`):

- **May import:** `app.domain`, `app.application`, and any third-party SDK.
- **Must NOT import:** `app.presentation`.

Adapters never import each other's layers upward. They are wired to the
application's use cases exclusively in [`app/core/container.py`](../core/container.py)
(the composition root). Presentation code depends on use cases, never on the
classes in this directory.

## Contents

| Path | Port implemented | Notes |
| --- | --- | --- |
| `persistence/in_memory/document_repository.py` | `IDocumentRepository` | Interim non-durable store; replaced by a SQLAlchemy/pgvector adapter in ticket #6. |
| `persistence/sql/tables.py` | — | SQLAlchemy Core tables mirroring the Alembic schema. Not used for DDL. |
| `persistence/sql/retrieval_store.py` | `IRetrievalStore` | pgvector cosine dense search + PostgreSQL `tsvector`/`ts_rank` keyword search (ADR-003). |
| `system/clock.py` | `IClock` | Wraps `datetime.now(UTC)`. |
| `system/identifiers.py` | `IIdGenerator` | Wraps `uuid.uuid4()`. |
