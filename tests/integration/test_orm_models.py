"""The ORM models must match the migrated schema (Ticket #6).

The models exist so ``alembic revision --autogenerate`` can diff them and write a
correct migration. That only holds while they genuinely mirror what the migrations
produce -- and a model edited out of step would silently generate a *wrong*
migration, which is worse than having no models at all.

So this migrates a scratch database to head and runs the same comparison
autogenerate uses, requiring it to find nothing. A column added to a model without
a migration, or a migration written without updating the models, fails here.

Database tests require ``TEST_DATABASE_URL`` pointing at a disposable PostgreSQL
service. They skip locally when it is absent, and fail in CI if unavailable.
The model-only checks always run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.engine import make_url

from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.domain.auth.value_objects import ResourceType, Role
from app.domain.jobs.entities import Job, JobState
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.job_store import PostgresJobStore, create_job_engine
from app.infrastructure.persistence.models import Base, JobModel, UserModel
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository
from tests.support.fakes import FixedClock, SequentialIdGenerator, build_user

_ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")
_SCRATCH_DB = "dc_ticket6_models_" + uuid4().hex


def _database_reachable() -> bool:
    import asyncio

    if not _ADMIN_URL:
        return False

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
    return make_url(_ADMIN_URL).set(database=_SCRATCH_DB).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A scratch database migrated to head, dropped on the way out."""
    if not _database_reachable():
        reason = "Set TEST_DATABASE_URL to a reachable disposable PostgreSQL service"
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(reason)
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
    context = MigrationContext.configure(connection, opts={"compare_server_default": True})
    return list(compare_metadata(context, Base.metadata))


class TestModelsMatchMigrations:
    def test_alembic_check_uses_the_shared_metadata(self, migrated_database: str) -> None:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migrated_database.replace("%", "%%"))
        with _preserving_logging():
            command.check(config)

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


class TestAuthJobModelIntegration:
    async def test_runner_results_are_readable_through_shared_models(
        self, migrated_database: str
    ) -> None:
        database = Database(migrated_database)
        engine = create_job_engine(migrated_database)
        identifiers = SequentialIdGenerator()
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        owner = build_user(
            user_id=identifiers.new_id(),
            email="orm-owner@example.com",
            role=Role.ANALYST,
            created_at=clock.now(),
        )
        job = Job(
            id=UUID(identifiers.new_id()),
            user_id=UUID(owner.id.value),
            operation_type="diagnostic",
            input_payload={},
            correlation_id=UUID(identifiers.new_id()),
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        try:
            await SqlUserRepository(database.session_factory).add(owner)
            store = PostgresJobStore(engine)
            await store.add(job)
            await store.transition(job.id, JobState.QUEUED, clock.now())
            runner = JobRunner(store, JobHandlerRegistry([DiagnosticJobHandler()]), clock)
            await runner.run(job.id)

            async with database.session_factory() as session:
                mapped_owner = await session.get(UserModel, UUID(owner.id.value))
                mapped_job = await session.get(JobModel, job.id)
                assert mapped_owner is not None
                assert mapped_owner.email == owner.email.value
                assert mapped_owner.hashed_password == owner.hashed_password
                assert mapped_job is not None
                assert mapped_job.user_id == mapped_owner.id
                assert mapped_job.state == JobState.COMPLETED.value
                assert mapped_job.operation_type == "diagnostic"
                assert mapped_job.input_payload == {}
                assert mapped_job.result_payload == {"ok": True}
                assert mapped_job.checkpoint_data == {"probe-v1": {"ok": True}}
                assert mapped_job.correlation_id == job.correlation_id
                assert mapped_job.started_at == clock.now()
                assert mapped_job.completed_at == clock.now()
                assert not mapped_job.cancellation_requested
            ownership = SqlOwnershipQuery(database.session_factory)
            assert await ownership.owner_of(ResourceType.JOB, str(job.id)) == owner.id
        finally:
            engine.dispose()
            await database.dispose()

    async def test_legacy_orm_rows_keep_migration_defaults_without_dispatch(
        self, migrated_database: str
    ) -> None:
        database = Database(migrated_database)
        engine = create_job_engine(migrated_database)
        owner_id = UUID(int=100)
        job_id = UUID(int=101)
        try:
            async with database.session_factory() as session:
                session.add(
                    UserModel(
                        id=owner_id,
                        email="legacy-orm-owner@example.com",
                        hashed_password="unused-test-digest",
                        role=Role.ANALYST.value,
                    )
                )
                await session.flush()
                legacy = JobModel(
                    id=job_id,
                    user_id=owner_id,
                    state=JobState.PENDING.value,
                    idempotency_key=str(job_id),
                    attempt_number=0,
                    max_attempts=3,
                    checkpoint_data={},
                )
                session.add(legacy)
                await session.commit()
                await session.refresh(legacy)
                assert legacy.operation_type is None
                assert legacy.input_payload == {}
                assert legacy.cancellation_requested is False
            assert job_id not in await PostgresJobStore(engine).dispatchable(100)
        finally:
            engine.dispose()
            await database.dispose()


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
