"""The ORM models must match the migrated schema (Ticket #6).

The models exist so ``alembic revision --autogenerate`` can diff them and write a
correct migration. That only holds while they genuinely mirror what the migrations
produce -- and a model edited out of step would silently generate a *wrong*
migration, which is worse than having no models at all.

So this migrates a scratch database to head and runs the same comparison
autogenerate uses, requiring it to find nothing. A column added to a model without
a migration, or a migration written without updating the models, fails here.

Skipped when no database is reachable, like the other PostgreSQL-backed tests.
``TEST_DATABASE_URL`` overrides the connection; the default matches
``docker-compose.yml``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect

from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Base

_ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
_SCRATCH_DB = "dc_ticket6_models_test"


def _database_reachable() -> bool:
    import asyncio

    try:
        import asyncpg
    except ImportError:
        return False

    async def probe() -> bool:
        try:
            connection = await asyncio.wait_for(asyncpg.connect(_ADMIN_URL), timeout=3)
        except Exception:
            return False
        await connection.close()
        return True

    try:
        return asyncio.run(probe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason=f"no PostgreSQL reachable at {_ADMIN_URL.rsplit('@', 1)[-1]}",
)


@contextmanager
def _preserving_logging() -> Iterator[None]:
    """Restore logger state around an in-process Alembic run.

    ``env.py`` passes ``disable_existing_loggers=False``, so this is
    belt-and-braces: it keeps an in-process migration from silencing the
    application's loggers for the rest of the session if that argument is ever
    dropped.
    """
    manager = logging.Logger.manager
    before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        yield
    finally:
        for name, logger in manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.disabled = before.get(name, False)


def _admin_sql(statement: str) -> None:
    import asyncio

    import asyncpg

    async def run() -> None:
        connection = await asyncpg.connect(_ADMIN_URL)
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def _scratch_url() -> str:
    base, _, _ = _ADMIN_URL.rpartition("/")
    return f"{base}/{_SCRATCH_DB}"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A scratch database migrated to head, dropped on the way out."""
    _admin_sql(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
    _admin_sql(f'CREATE DATABASE "{_SCRATCH_DB}"')
    try:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("DATABASE__URL", _scratch_url())
        try:
            with _preserving_logging():
                command.upgrade(Config("alembic.ini"), "head")
        finally:
            monkeypatch.undo()
        yield _scratch_url()
    finally:
        _admin_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{_SCRATCH_DB}' AND pid <> pg_backend_pid()"
        )
        _admin_sql(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection)
    return list(compare_metadata(context, Base.metadata))


class TestModelsMatchMigrations:
    async def test_autogenerate_finds_no_difference(self, migrated_database: str) -> None:
        # The whole point of the models. If this fails, `alembic revision
        # --autogenerate` would emit a migration for a difference that is really a
        # modelling mistake.
        database = Database(migrated_database)
        try:
            async with database.session_factory() as session:
                connection = await session.connection()
                differences = await connection.run_sync(_diff)
        finally:
            await database.dispose()

        assert differences == [], (
            "models and migrations have drifted; autogenerate reports:\n"
            + "\n".join(repr(d) for d in differences)
        )

    async def test_every_migrated_table_has_a_model(self, migrated_database: str) -> None:
        # compare_metadata would catch a missing table, but this failure message
        # names it directly.
        database = Database(migrated_database)
        try:
            async with database.session_factory() as session:
                connection = await session.connection()
                tables = await connection.run_sync(
                    lambda sync_conn: set(inspect(sync_conn).get_table_names())
                )
        finally:
            await database.dispose()

        tables.discard("alembic_version")  # Alembic's own bookkeeping, never mapped.
        assert tables == set(Base.metadata.tables)


class TestModelDefinitions:
    """Checks that need no database."""

    def test_the_auth_tables_ticket_5_depends_on_are_mapped(self) -> None:
        # The ownership checks read user_id from these; a missing mapping here
        # would mean a repository could not be written against them.
        for table in ("users", "workflow_runs", "jobs", "traces", "sessions", "documents"):
            assert table in Base.metadata.tables, f"{table} is not mapped"

    def test_every_ownership_checked_table_exposes_user_id(self) -> None:
        for table in ("workflow_runs", "jobs", "traces", "sessions", "documents"):
            columns = Base.metadata.tables[table].columns
            assert "user_id" in columns, f"{table} has no user_id"
            assert not columns["user_id"].nullable, (
                f"{table}.user_id must be NOT NULL: an unowned row could never be "
                "authorized, since an absent owner is never treated as a match"
            )

    def test_the_metadata_columns_are_mapped_around_the_reserved_name(self) -> None:
        # `metadata` collides with DeclarativeBase.metadata, so the attribute is
        # metadata_ while the column keeps its real name.
        for table in ("documents", "chunks"):
            assert "metadata" in Base.metadata.tables[table].columns

    def test_user_email_is_unique(self) -> None:
        # The uniqueness the login lookup and demo seeding both rely on.
        assert Base.metadata.tables["users"].columns["email"].unique is True

    def test_no_model_declares_a_plaintext_password_column(self) -> None:
        # Constraint C6 / AC-8.1 at the schema level: only a digest is storable.
        for table_name, table in Base.metadata.tables.items():
            for column in table.columns:
                assert column.name != "password", (
                    f"{table_name}.{column.name} would store a plaintext password"
                )
