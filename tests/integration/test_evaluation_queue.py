"""Real PG/pgvector/FTS/Redis/Celery/HTTP plus kill/resume/cancel verification."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import redis
import sqlalchemy as sa
from fastapi import FastAPI

from app.core.config import DatabaseSettings, QueueSettings, Settings
from app.domain.auth.value_objects import Role
from app.domain.jobs.entities import JobState
from app.infrastructure.persistence.models import (
    ChunkEmbeddingModel,
    ChunkModel,
    DocumentModel,
    DocumentSourceModel,
)
from app.presentation.api.dependencies import get_evaluation_service
from tests.integration.conftest import auth
from tests.integration.evaluation_worker import SOURCE_HASH, QueueTestCatalog, build_test_runtime
from tests.integration.test_job_queue import Environment, migrate, worker
from tests.integration.test_jobs_api import JobAPI
from tests.integration.test_jobs_api import api as api
from tests.support.evaluation_fakes import CHUNK, TEXT


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    source = os.environ.get("TEST_DATABASE_URL")
    if not source or not os.environ.get("T20_TEST_REDIS_URL"):
        if os.environ.get("CI"):
            pytest.fail("CI must provide PostgreSQL and Redis for evaluation integration")
        pytest.skip("Set TEST_DATABASE_URL and T20_TEST_REDIS_URL")
    name = "t12_evaluation_" + uuid4().hex
    admin = sa.engine.make_url(source).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(admin, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        migrate(admin.set(database=name).render_as_string(hide_password=False))
        # Plain URLs select the app's asyncpg driver on Windows and Linux.
        yield admin.set(database=name, drivername="postgresql").render_as_string(
            hide_password=False
        )
    finally:
        with engine.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()


@pytest.fixture
def jobs(database_url: str) -> Iterator[Environment]:
    broker = os.environ["T20_TEST_REDIS_URL"]
    queue = "t12_eval_" + uuid4().hex
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=database_url),
        queue=QueueSettings(broker_url=broker, default_queue=queue, publish_timeout_seconds=0.5),
    )
    runtime = build_test_runtime(settings)
    owner = uuid4()
    with runtime.engine.begin() as connection:
        # This table exists only in the disposable test database, never the app schema.
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS evaluation_test_calls "
                "(query TEXT PRIMARY KEY, executions INTEGER NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO users(id,email,hashed_password,role) "
                "VALUES (:id,:email,'test-hash','admin')"
            ),
            {"id": owner, "email": f"{owner}@example.com"},
        )
        connection.execute(
            sa.insert(DocumentModel).values(
                id=UUID(CHUNK.document_id),
                user_id=owner,
                filename=CHUNK.document_name,
                content="",
                status="COMPLETED",
                metadata_={},
                content_hash=SOURCE_HASH,
                media_type="text/markdown",
                version=1,
                chunk_count=1,
                ingestion_config={
                    "embedding_model": "all-MiniLM-L6-v2",
                    "embedding_dim": 384,
                    "embedding_version": "1",
                },
            )
        )
        connection.execute(
            sa.insert(DocumentSourceModel).values(
                document_id=UUID(CHUNK.document_id), source=TEXT.encode()
            )
        )
        connection.execute(
            sa.insert(ChunkModel).values(
                id=UUID(CHUNK.chunk_id),
                document_id=UUID(CHUNK.document_id),
                text=TEXT,
                section=CHUNK.section,
                page=None,
                metadata_={},
                document_version=1,
            )
        )
        connection.execute(
            sa.insert(ChunkEmbeddingModel).values(
                id=uuid4(),
                chunk_id=UUID(CHUNK.chunk_id),
                embedding=[1.0] + [0.0] * 383,
                embedding_model="all-MiniLM-L6-v2",
                embedding_version="1",
                embedding_dim=384,
            )
        )
    try:
        yield Environment(runtime, owner, database_url, broker, queue)
    finally:
        with runtime.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM evaluation_test_calls"))
            connection.execute(sa.text("DELETE FROM documents"))
            connection.execute(sa.text("DELETE FROM traces"))
            connection.execute(sa.text("DELETE FROM jobs"))
            connection.execute(sa.text("DELETE FROM users"))
        with redis.Redis.from_url(broker) as client:
            client.delete(queue, "_kombu.binding." + queue)
        asyncio.run(runtime.close())


@pytest.fixture
def evaluation_api(api: JobAPI, jobs: Environment) -> JobAPI:
    cast(FastAPI, api.client.app).dependency_overrides[get_evaluation_service] = lambda: (
        jobs.runtime.evaluation
    )
    return api


def submit(api: JobAPI) -> UUID:
    response = api.client.post("/api/v1/evaluations", headers=auth(api.tokens[Role.ADMIN]))
    assert response.status_code == 202, response.text
    assert response.headers["Location"] == response.json()["status_url"]
    return UUID(response.json()["job_id"])


async def wait_for_artifact(jobs: Environment, job_id: UUID) -> None:
    for _ in range(150):
        with jobs.runtime.engine.connect() as connection:
            count = connection.scalar(
                sa.text(
                    "SELECT count(*) FROM job_events WHERE job_id=:id "
                    "AND payload->>'case_id'='qa-one'"
                ),
                {"id": job_id},
            )
        if count:
            return
        job = await jobs.runtime.service.get(job_id)
        if job.terminal:
            with jobs.runtime.engine.connect() as connection:
                error = connection.scalar(
                    sa.text(
                        "SELECT payload FROM job_events "
                        "WHERE job_id=:id AND event_type='evaluation.failure'"
                    ),
                    {"id": job_id},
                )
            pytest.fail(f"Evaluation ended before its first case: {job.state}, {error}")
        await asyncio.sleep(0.1)
    pytest.fail("Evaluation case did not commit")


async def wait_for(jobs: Environment, job_id: UUID, target: JobState) -> None:
    for _ in range(200):
        job = await jobs.runtime.service.get(job_id)
        if job.state == target:
            return
        if job.terminal:
            with jobs.runtime.engine.connect() as connection:
                error = connection.scalar(
                    sa.text(
                        "SELECT payload FROM job_events "
                        "WHERE job_id=:id AND event_type='evaluation.failure'"
                    ),
                    {"id": job_id},
                )
            pytest.fail(f"Evaluation ended unexpectedly: {job.state}, {error}")
        await asyncio.sleep(0.1)
    pytest.fail(f"Evaluation did not reach {target}; state={job.state}")


def assert_each_case_executed_once(jobs: Environment) -> None:
    with jobs.runtime.engine.connect() as connection:
        calls: dict[str, int] = dict(
            connection.execute(sa.text("SELECT query, executions FROM evaluation_test_calls")).all()
        )
    assert calls == {case.query: 1 for case in QueueTestCatalog().load().cases}


async def test_http_async_report_uses_real_pg_retrieval_and_existing_spans(
    evaluation_api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    api = evaluation_api
    with worker(jobs, tmp_path, module="tests.integration.evaluation_worker"):
        identifier = submit(api)
        await wait_for(jobs, identifier, JobState.COMPLETED)
    result = api.client.get(
        f"/api/v1/evaluations/{identifier}/report", headers=auth(api.tokens[Role.ADMIN])
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["summary"]["executed_cases"] == 3
    assert data["summary"]["passed_cases"] == 3
    first = data["cases"][0]
    assert first["retrieval"]["keyword_count"] == 1
    assert first["retrieval"]["dense_count"] == 1
    assert first["retrieval"]["rrf_k"] == 60
    assert first["citations"][0]["chunk_id"] == CHUNK.chunk_id
    assert_each_case_executed_once(jobs)
    with jobs.runtime.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM spans WHERE name='qa.ask'")) == 3
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM job_events WHERE event_type='evaluation.case'")
            )
            == 3
        )
    rendered = api.client.get(
        f"/api/v1/evaluations/{identifier}/report?format=markdown",
        headers=auth(api.tokens[Role.ADMIN]),
    )
    assert rendered.status_code == 200 and "100.00%" in rendered.text
    status = api.client.get(
        f"/api/v1/evaluations/{identifier}", headers=auth(api.tokens[Role.ADMIN])
    ).json()
    assert status["completed_cases"] == status["total_cases"] == 3
    assert (
        api.client.get(
            f"/api/v1/evaluations/{identifier}/report", headers=auth(api.tokens[Role.ANALYST])
        ).status_code
        == 403
    )


async def test_process_death_after_case_commit_resumes_without_repeating_a_case(
    evaluation_api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    api = evaluation_api
    with worker(
        jobs,
        tmp_path,
        module="tests.integration.evaluation_worker",
        environment_overrides={"T12_TEST_PAUSE_AFTER_ARTIFACT": "case/qa-one"},
    ) as process:
        identifier = submit(api)
        await wait_for_artifact(jobs, identifier)
        process.kill()
        process.wait(timeout=10)
    job = await jobs.runtime.service.get(identifier)
    assert job.state == JobState.STARTED
    assert "evaluation/case/qa-one" not in job.checkpoint_data
    response = api.client.post(
        f"/api/v1/evaluations/{identifier}/restart", headers=auth(api.tokens[Role.ADMIN])
    )
    assert response.status_code == 202 and response.json()["job_id"] == str(identifier)
    with worker(jobs, tmp_path, module="tests.integration.evaluation_worker"):
        await wait_for(jobs, identifier, JobState.COMPLETED)
    # Trace persistence is deliberately best effort and can time out under load.
    # Use the test's durable execution counter to prove the case was not rerun.
    assert_each_case_executed_once(jobs)


async def test_cancel_flag_survives_worker_loss_and_terminal_restart_keeps_prior_results(
    evaluation_api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    api = evaluation_api
    with worker(
        jobs,
        tmp_path,
        module="tests.integration.evaluation_worker",
        environment_overrides={"T12_TEST_PAUSE_AFTER_ARTIFACT": "case/qa-one"},
    ) as process:
        identifier = submit(api)
        await wait_for_artifact(jobs, identifier)
        assert (
            api.client.post(
                f"/api/v1/evaluations/{identifier}/cancel", headers=auth(api.tokens[Role.ADMIN])
            ).status_code
            == 202
        )
        process.kill()
        process.wait(timeout=10)
    assert (await jobs.runtime.service.get(identifier)).cancellation_requested
    api.client.post(
        f"/api/v1/evaluations/{identifier}/restart", headers=auth(api.tokens[Role.ADMIN])
    )
    with worker(jobs, tmp_path, module="tests.integration.evaluation_worker"):
        await wait_for(jobs, identifier, JobState.CANCELLED)
        response = api.client.post(
            f"/api/v1/evaluations/{identifier}/restart", headers=auth(api.tokens[Role.ADMIN])
        )
        assert response.status_code == 202
        child = UUID(response.json()["job_id"])
        assert child != identifier
        await wait_for(jobs, child, JobState.COMPLETED)
    assert_each_case_executed_once(jobs)
    assert (await jobs.runtime.service.get(identifier)).state == JobState.CANCELLED


def test_only_admin_can_submit_or_control_an_evaluation(evaluation_api: JobAPI) -> None:
    api = evaluation_api
    assert api.client.post("/api/v1/evaluations").status_code == 401
    for role in (Role.ANALYST, Role.REVIEWER):
        assert (
            api.client.post("/api/v1/evaluations", headers=auth(api.tokens[role])).status_code
            == 403
        )
    identifier = submit(api)
    for action in ("cancel", "restart"):
        assert (
            api.client.post(
                f"/api/v1/evaluations/{identifier}/{action}", headers=auth(api.tokens[Role.ANALYST])
            ).status_code
            == 403
        )
