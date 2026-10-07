"""Shared fixtures for the integration tests.

Drives the real DI chain with the real adapters: bcrypt hashing, signed JWTs, the
composition root, and FastAPI's dependency injection. Nothing here is mocked --
the point of these tests is that the parts fit together.

Fixtures are module-scoped because seeding three demo accounts costs three real
bcrypt hashes, and a token per role costs one more each. ``AUTH__BCRYPT_ROUNDS``
is set to the adapter's minimum for the same reason. Tests that need per-test
isolation register their own resources under unique ids rather than rebuilding the
container.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.service import JobService
from app.core.config import get_settings
from app.core.container import get_container
from app.domain.auth.value_objects import EmailAddress, ResourceType, Role, UserId
from app.domain.jobs.entities import Job
from app.infrastructure.persistence.in_memory.ownership_query import (
    InMemoryOwnershipQuery,
)
from app.presentation.api.app import create_app
from app.presentation.api.dependencies import get_job_service
from tests.support.fakes import FixedClock, SequentialIdGenerator
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

_ownership_jobs = FakeJobStore()

#: Development-only credential for the seeded demo accounts. Supplied through the
#: environment exactly as a developer would supply it, so the tests exercise the
#: real seeding path rather than a special test-only shortcut.
DEMO_PASSWORD = "integration-test-password-1234"

_SIGNING_SECRET = "integration-test-signing-secret-value"

DEMO_EMAILS = {
    Role.ANALYST: "analyst@example.com",
    Role.REVIEWER: "reviewer@example.com",
    Role.ADMIN: "admin@example.com",
}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """A TestClient over the real app, with demo accounts seeded on startup."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH__SECRET_KEY", _SIGNING_SECRET)
    monkeypatch.setenv("AUTH__DEMO_PASSWORD", DEMO_PASSWORD)
    monkeypatch.setenv("AUTH__SEED_DEMO_ACCOUNTS", "true")
    monkeypatch.setenv("AUTH__BCRYPT_ROUNDS", "10")
    # Hermetic regardless of the developer's .env: this module exercises the HTTP
    # surface on in-memory adapters. Durability against PostgreSQL is proven in
    # test_sql_ownership.py.
    monkeypatch.delenv("DATABASE__URL", raising=False)

    get_settings.cache_clear()
    get_container.cache_clear()
    # Entering the context manager runs the lifespan hook, which seeds.
    app = create_app()
    # These legacy RBAC tests exercise the real authorization service on memory
    # adapters. Ticket 20's API/worker suite separately uses real PostgreSQL/Redis.
    job_service = JobService(
        _ownership_jobs,
        FakeJobQueue(_ownership_jobs),
        JobHandlerRegistry([DiagnosticJobHandler()]),
        FixedClock(datetime.now(UTC)),
        SequentialIdGenerator(),
    )
    app.dependency_overrides[get_job_service] = lambda: job_service
    with TestClient(app) as test_client:
        yield test_client

    _ownership_jobs.jobs.clear()
    monkeypatch.undo()
    get_settings.cache_clear()
    get_container.cache_clear()


def login(client: TestClient, role: Role, password: str = DEMO_PASSWORD) -> str:
    """Authenticate as the demo account for ``role`` and return its access token."""
    response = client.post(
        "/api/v1/auth/token",
        json={"email": DEMO_EMAILS[role], "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    """Build the ``Authorization`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def tokens(client: TestClient) -> dict[Role, str]:
    """One access token per role."""
    return {role: login(client, role) for role in Role}


@pytest.fixture(scope="module")
def user_ids(client: TestClient) -> dict[Role, UserId]:
    """The seeded user id for each role, read back from the server."""
    ids: dict[Role, UserId] = {}
    for role in Role:
        response = client.get("/api/v1/auth/me", headers=auth(login(client, role)))
        assert response.status_code == 200, response.text
        ids[role] = UserId(response.json()["id"])
    return ids


def own(resource_type: ResourceType, resource_id: str, owner: UserId) -> None:
    """Record ``owner`` as the owner of ``resource_id`` in the live container.

    Ownership rows are normally written by whatever creates a run, job, trace or
    session -- features that belong to later tickets. Until then this is how a test
    arranges a persisted owner for the guard to find.

    These tests run on the in-memory adapter (no ``DATABASE__URL``), which is why
    this can reach for ``register``. The PostgreSQL-backed equivalents live in
    ``test_sql_ownership.py`` and insert real rows instead.
    """
    query = get_container().ownership_query
    assert isinstance(query, InMemoryOwnershipQuery), (
        "integration fixtures expect the in-memory ownership adapter; "
        "DATABASE__URL must not be set for this module"
    )
    query.register(resource_type, resource_id, owner)
    if resource_type == ResourceType.JOB:
        now = datetime.now(UTC)
        identifier = UUID(resource_id)
        _ownership_jobs.jobs[identifier] = Job(
            id=identifier,
            operation_type="diagnostic",
            input_payload={},
            user_id=UUID(owner.value),
            created_at=now,
            updated_at=now,
        )


async def find_user_id(email: str) -> UserId:
    """Look up a seeded account's id directly from the repository."""
    user = await get_container().user_repository.get_by_email(EmailAddress(email))
    assert user is not None, f"demo account {email} was not seeded"
    return user.id
