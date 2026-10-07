"""Object ownership against real PostgreSQL (BRD AC-8.4, SEC-1a).

The other ownership tests prove the *decision* is server-side. These prove the
*store* is durable: that ownership is a row in PostgreSQL which outlives the
process that wrote it, not a dict that dies with it.

Each run creates a scratch database, migrates it to head, and drops it afterwards,
so nothing touches a developer's own data.

``TEST_DATABASE_URL`` must point at a disposable PostgreSQL service. These tests
skip locally when it is absent and **fail in CI**, matching
``test_orm_models.py``: a durability suite that silently stops running is worse
than one that is absent, because it still looks like coverage.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.container import get_container
from app.domain.auth.value_objects import EmailAddress, ResourceType, Role, UserId
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository
from app.presentation.api.app import create_app
from tests.integration.conftest import DEMO_PASSWORD, auth
from tests.support.fakes import build_user

#: No default credentials: ``docker-compose.yml`` sets
#: ``POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-}``, so there is no password this
#: could usefully guess. An unset variable is reported, not silently worked around.
_ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")
_SCRATCH_DB = "dc_ticket5_ownership_test"
_SIGNING_SECRET = "sql-ownership-test-signing-secret-value"

_RESOURCE_TABLES = {
    ResourceType.RUN: "workflow_runs",
    ResourceType.JOB: "jobs",
    ResourceType.TRACE: "traces",
    ResourceType.SESSION: "sessions",
}

# Minimal INSERTs satisfying each table's NOT NULL columns. Ownership is the only
# column under test; the rest is whatever the schema demands.
_INSERT_BY_RESOURCE = {
    ResourceType.RUN: text(
        """
        INSERT INTO workflow_runs (id, user_id, correlation_id, case_summary, state)
        VALUES (:id, :user_id, :text_id, 'test case', 'AWAITING_APPROVAL')
        """
    ),
    ResourceType.JOB: text(
        """
        INSERT INTO jobs (id, user_id, state, idempotency_key, attempt_number,
                          max_attempts, checkpoint_data)
        VALUES (:id, :user_id, 'PENDING', :text_id, 0, 3, '{}'::jsonb)
        """
    ),
    ResourceType.TRACE: text("INSERT INTO traces (id, user_id) VALUES (:id, :user_id)"),
    ResourceType.SESSION: text(
        "INSERT INTO sessions (id, user_id, title) VALUES (:id, :user_id, 'test session')"
    ),
}


def _database_reachable() -> bool:
    import asyncio

    import asyncpg

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


def _require_database() -> None:
    """Skip locally, but fail in CI, when no disposable database is reachable.

    A silent skip in CI would mean Ticket 5's durability guarantee stopped being
    checked while still appearing to be covered.
    """
    if _database_reachable():
        return
    reason = "Set TEST_DATABASE_URL to a reachable disposable PostgreSQL service"
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@contextmanager
def _preserving_logging() -> Iterator[None]:
    """Restore logger state around an in-process Alembic run.

    ``migrations/env.py`` calls ``logging.config.fileConfig``, whose default
    ``disable_existing_loggers=True`` switches off every logger already created --
    including the application's. That silenced ``app.core.container`` for the rest
    of the pytest session and made unrelated ``caplog`` assertions fail. env.py
    now passes ``disable_existing_loggers=False``, so this is belt-and-braces: it
    keeps an in-process migration from reaching outside this module if that
    argument is ever dropped.
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
    _require_database()
    _admin_sql(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
    _admin_sql(f'CREATE DATABASE "{_SCRATCH_DB}"')
    try:
        # No set_main_option and no -x override: the URL is resolved by env.py
        # from DATABASE__URL exactly as it is for an operator running
        # `alembic upgrade head`. If that wiring breaks, this fixture fails.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("DATABASE__URL", _scratch_url())
        try:
            # Alembic's env.py calls asyncio.run itself, so this must not run
            # inside a loop -- which is why the fixture is sync.
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


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    """A Database whose pool is opened and closed inside the test's own loop.

    Disposing from a separate ``asyncio.run`` would try to close asyncpg
    connections belonging to a loop that no longer exists, which raises and leaves
    the scratch database in use so it cannot be dropped.
    """
    db = Database(migrated_database)
    yield db
    await db.dispose()


async def _seed_user(db: Database, role: Role) -> UserId:
    repository = SqlUserRepository(db.session_factory)
    user = build_user(
        user_id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex}@example.com",
        role=role,
        created_at=datetime.now(UTC),
    )
    await repository.add(user)
    return user.id


async def _insert_resource(db: Database, kind: ResourceType, owner: UserId) -> str:
    resource_id = str(uuid.uuid4())
    async with db.session_factory() as session:
        await session.execute(
            _INSERT_BY_RESOURCE[kind],
            {
                "id": uuid.UUID(resource_id),
                "text_id": resource_id,
                "user_id": uuid.UUID(owner.value),
            },
        )
        await session.commit()
    return resource_id


class TestOwnershipIsPersisted:
    @pytest.mark.parametrize("kind", list(_RESOURCE_TABLES))
    async def test_the_owner_is_read_back_from_the_database(
        self, database: Database, kind: ResourceType
    ) -> None:
        owner = await _seed_user(database, Role.ANALYST)
        resource_id = await _insert_resource(database, kind, owner)

        query = SqlOwnershipQuery(database.session_factory)
        assert await query.owner_of(kind, resource_id) == owner

    @pytest.mark.parametrize("kind", list(_RESOURCE_TABLES))
    async def test_ownership_survives_a_new_engine(
        self, migrated_database: str, kind: ResourceType
    ) -> None:
        # The point of the whole exercise. Write through one engine, dispose it,
        # read through a completely separate one -- which is what a process
        # restart looks like from the data's point of view.
        writer = Database(migrated_database)
        owner = await _seed_user(writer, Role.ANALYST)
        resource_id = await _insert_resource(writer, kind, owner)
        await writer.dispose()

        reader = Database(migrated_database)
        try:
            found = await SqlOwnershipQuery(reader.session_factory).owner_of(kind, resource_id)
        finally:
            await reader.dispose()

        assert found == owner, "ownership did not survive a new connection pool"

    @pytest.mark.parametrize("kind", list(_RESOURCE_TABLES))
    async def test_an_unknown_id_has_no_owner(self, database: Database, kind: ResourceType) -> None:
        query = SqlOwnershipQuery(database.session_factory)
        assert await query.owner_of(kind, str(uuid.uuid4())) is None

    @pytest.mark.parametrize(
        "hostile", ["", "   ", "not-a-uuid", "1", "null", "' OR '1'='1", "../../etc"]
    )
    async def test_a_malformed_id_is_a_miss_not_a_database_error(
        self, database: Database, hostile: str
    ) -> None:
        # The id columns are uuid, so handing PostgreSQL a non-UUID would raise
        # InvalidTextRepresentation and surface as a 500 on an authorization path.
        query = SqlOwnershipQuery(database.session_factory)
        assert await query.owner_of(ResourceType.RUN, hostile) is None

    async def test_resource_types_do_not_bleed_into_each_other(self, database: Database) -> None:
        # A run id must not resolve as a job id, even though both tables have the
        # same column names.
        owner = await _seed_user(database, Role.ANALYST)
        run_id = await _insert_resource(database, ResourceType.RUN, owner)

        query = SqlOwnershipQuery(database.session_factory)
        assert await query.owner_of(ResourceType.RUN, run_id) == owner
        assert await query.owner_of(ResourceType.JOB, run_id) is None


class TestUserIdentityIsPersisted:
    async def test_a_user_round_trips_through_the_database(self, database: Database) -> None:
        repository = SqlUserRepository(database.session_factory)
        email = f"{uuid.uuid4().hex}@example.com"
        original = build_user(
            user_id=str(uuid.uuid4()),
            email=email,
            role=Role.REVIEWER,
            created_at=datetime.now(UTC),
        )
        await repository.add(original)

        loaded = await repository.get_by_email(EmailAddress(email))
        assert loaded is not None
        assert loaded.id == original.id
        assert loaded.role is Role.REVIEWER
        assert loaded.hashed_password == original.hashed_password
        assert loaded.created_at.tzinfo is not None

    async def test_the_stored_role_is_what_a_later_read_returns(self, database: Database) -> None:
        # The basis of "the stored role is authoritative": change it in the
        # database and the next read reflects it, with no token involved.
        repository = SqlUserRepository(database.session_factory)
        user_id = await _seed_user(database, Role.ANALYST)

        async with database.session_factory() as session:
            await session.execute(
                text("UPDATE users SET role = 'admin' WHERE id = :id"),
                {"id": uuid.UUID(user_id.value)},
            )
            await session.commit()

        reloaded = await repository.get_by_id(user_id)
        assert reloaded is not None
        assert reloaded.role is Role.ADMIN

    async def test_an_unknown_user_is_none(self, database: Database) -> None:
        repository = SqlUserRepository(database.session_factory)
        assert await repository.get_by_id(UserId(str(uuid.uuid4()))) is None
        assert await repository.get_by_email(EmailAddress("nobody@example.com")) is None


class TestEndToEndWithPostgres:
    """The HTTP surface, with the SQL adapters actually wired into the container."""

    @pytest.fixture
    def sql_client(self, migrated_database: str) -> Iterator[TestClient]:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("AUTH__SECRET_KEY", _SIGNING_SECRET)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", DEMO_PASSWORD)
        monkeypatch.setenv("AUTH__BCRYPT_ROUNDS", "10")
        monkeypatch.setenv("DATABASE__URL", migrated_database)
        get_settings.cache_clear()
        get_container.cache_clear()
        with TestClient(create_app()) as client:
            yield client
        monkeypatch.undo()
        get_settings.cache_clear()
        get_container.cache_clear()

    def test_the_container_is_running_on_postgres(self, sql_client: TestClient) -> None:
        assert isinstance(get_container().ownership_query, SqlOwnershipQuery)
        assert isinstance(get_container().user_repository, SqlUserRepository)

    def test_demo_seeding_wrote_rows_to_the_database(self, sql_client: TestClient) -> None:
        response = sql_client.post(
            "/api/v1/auth/token",
            json={"email": "analyst@example.com", "password": DEMO_PASSWORD},
        )
        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("kind", "path"),
        [
            (ResourceType.RUN, "runs"),
            (ResourceType.JOB, "jobs"),
            (ResourceType.TRACE, "traces"),
            (ResourceType.SESSION, "sessions"),
        ],
    )
    def test_ownership_is_enforced_against_persisted_rows(
        self, sql_client: TestClient, kind: ResourceType, path: str
    ) -> None:
        import asyncio

        def token_for(email: str) -> str:
            response = sql_client.post(
                "/api/v1/auth/token", json={"email": email, "password": DEMO_PASSWORD}
            )
            assert response.status_code == 200, response.text
            return str(response.json()["access_token"])

        analyst_token = token_for("analyst@example.com")
        analyst_id = UserId(
            sql_client.get("/api/v1/auth/me", headers=auth(analyst_token)).json()["id"]
        )

        url = get_container().settings.database.url
        assert url is not None

        async def arrange() -> tuple[str, str]:
            # A dedicated engine for the arrangement: the container's own pool
            # belongs to the TestClient's loop and must not be touched from here.
            db = Database(url)
            try:
                mine = await _insert_resource(db, kind, analyst_id)
                stranger = await _seed_user(db, Role.ANALYST)
                theirs = await _insert_resource(db, kind, stranger)
                return mine, theirs
            finally:
                await db.dispose()

        owned, not_owned = asyncio.run(arrange())

        assert (
            sql_client.get(f"/api/v1/{path}/{owned}", headers=auth(analyst_token)).status_code
            == 200
        )
        assert (
            sql_client.get(f"/api/v1/{path}/{not_owned}", headers=auth(analyst_token)).status_code
            == 403
        )
        assert (
            sql_client.get(
                f"/api/v1/{path}/{uuid.uuid4()}", headers=auth(analyst_token)
            ).status_code
            == 404
        )
        assert sql_client.get(f"/api/v1/{path}/{owned}").status_code == 401
