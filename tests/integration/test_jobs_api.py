"""JWT-authenticated HTTP jobs backed by real PostgreSQL, Redis and Celery."""

from __future__ import annotations

import asyncio
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.container import get_container
from app.domain.auth.value_objects import Role
from app.domain.jobs.entities import JobState
from app.presentation.api.app import create_app
from tests.integration.conftest import auth
from tests.integration.test_job_queue import Environment, wait_for, worker
from tests.integration.test_job_queue import database_url as database_url
from tests.integration.test_job_queue import jobs as jobs


@dataclass
class JobAPI:
    client: TestClient
    tokens: dict[Role, str]
    owners: dict[Role, UUID]


@pytest.fixture
def api(jobs: Environment, monkeypatch: pytest.MonkeyPatch) -> Iterator[JobAPI]:
    password = secrets.token_urlsafe(24)
    for key, value in {
        "DATABASE__URL": jobs.url,
        "QUEUE__BROKER_URL": jobs.broker,
        "QUEUE__DEFAULT_QUEUE": jobs.queue,
        "AUTH__SECRET_KEY": secrets.token_urlsafe(48),
        "AUTH__DEMO_PASSWORD": password,
        "AUTH__SEED_DEMO_ACCOUNTS": "true",
        "AUTH__BCRYPT_ROUNDS": "10",
        "ENVIRONMENT": "development",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_container.cache_clear()
    # psycopg async uses a selector on Windows; production workers run on Linux.
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    try:
        with TestClient(create_app(), backend_options=options) as client:
            tokens = {}
            owners = {}
            for role in Role:
                response = client.post(
                    "/api/v1/auth/token",
                    json={"email": f"{role.value}@example.com", "password": password},
                )
                assert response.status_code == 200, response.text
                token = response.json()["access_token"]
                tokens[role] = token
                owners[role] = UUID(client.get("/api/v1/auth/me", headers=auth(token)).json()["id"])
            yield JobAPI(client, tokens, owners)
    finally:
        get_settings.cache_clear()
        get_container.cache_clear()


async def test_http_submission_returns_202_and_polling_returns_postgres_result(
    api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        accepted = api.client.post(
            "/api/v1/jobs",
            headers=auth(api.tokens[Role.ADMIN]),
            json={"operation_type": "diagnostic", "payload": {}},
        )
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        identifier = UUID(body["job_id"])
        assert body["state"] == "QUEUED"
        assert accepted.headers["location"] == body["status_url"]
        await wait_for(jobs, identifier, JobState.COMPLETED)
    response = api.client.get(body["status_url"], headers=auth(api.tokens[Role.ADMIN]))
    assert response.status_code == 200
    status = response.json()
    assert status["state"] == "COMPLETED" and status["result"] == {"ok": True}
    assert status["error"] is None
    assert status["owner_id"] == str(api.owners[Role.ADMIN])
    assert status["started_at"] and status["completed_at"] and status["correlation_id"]
    assert {"input_payload", "checkpoint_data", "access_token"}.isdisjoint(status)


@pytest.mark.parametrize("role", [Role.ANALYST, Role.REVIEWER])
def test_generic_job_submission_requires_admin_permission(api: JobAPI, role: Role) -> None:
    response = api.client.post(
        "/api/v1/jobs", headers=auth(api.tokens[role]), json={"operation_type": "diagnostic"}
    )
    assert response.status_code == 403


async def test_job_polling_checks_the_persisted_owner_and_admin_grant(
    api: JobAPI, jobs: Environment
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=api.owners[Role.ANALYST])
    url = f"/api/v1/jobs/{job.id}"
    for role, expected in [(Role.ANALYST, 200), (Role.REVIEWER, 403), (Role.ADMIN, 200)]:
        response = api.client.get(url, headers=auth(api.tokens[role]))
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.json()["state"] == "QUEUED"
            assert response.json()["result"] is None and response.json()["error"] is None
    assert api.client.get(url).status_code == 401
    assert (
        api.client.get(
            url + f"?owner_id={api.owners[Role.REVIEWER]}&role=admin",
            headers={**auth(api.tokens[Role.REVIEWER]), "X-Role": "admin"},
        ).status_code
        == 403
    )
    assert (
        api.client.get(f"/api/v1/jobs/{uuid4()}", headers=auth(api.tokens[Role.ADMIN])).status_code
        == 404
    )


def test_unauthenticated_submission_is_refused(api: JobAPI) -> None:
    response = api.client.post("/api/v1/jobs", json={"operation_type": "diagnostic"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "body",
    [
        {"operation_type": "not.registered"},
        {"operation_type": "diagnostic", "user_id": str(UUID(int=1))},
        {"operation_type": "diagnostic", "payload": []},
        {"operation_type": "diagnostic", "payload": {"too_big": "x" * 65_536}},
    ],
    ids=["unknown-handler", "forged-owner", "non-object", "oversized-payload"],
)
def test_invalid_submission_creates_no_job(
    api: JobAPI, jobs: Environment, body: dict[str, Any]
) -> None:
    response = api.client.post("/api/v1/jobs", json=body, headers=auth(api.tokens[Role.ADMIN]))
    assert response.status_code == 422
    with jobs.runtime.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM jobs")) == 0


async def test_http_polling_returns_safe_failure_from_postgres(
    api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit("test.failure", {}, user_id=api.owners[Role.ADMIN])
        await wait_for(jobs, job.id, JobState.FAILED)
    response = api.client.get(f"/api/v1/jobs/{job.id}", headers=auth(api.tokens[Role.ADMIN]))
    assert response.status_code == 200
    assert response.json()["error"] == "JOB_HANDLER_FAILED"
    assert response.json()["state"] == "FAILED" and response.json()["result"] is None
    assert "private handler diagnostic" not in response.text
