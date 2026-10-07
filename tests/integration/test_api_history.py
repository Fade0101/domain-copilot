"""Ticket #24 history, ownership, atomicity and job retry against PostgreSQL/Redis."""

import asyncio
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.application.qa.use_cases import AskResult
from app.application.sessions import HistoryStoreUnavailableError
from app.core.config import DatabaseSettings, QueueSettings, Settings
from app.core.container import build_job_runtime
from app.domain.auth.value_objects import Role
from app.domain.jobs.entities import JobState
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Base
from app.infrastructure.persistence.sql.session_store import PostgresSessionStore
from app.presentation.api.dependencies import get_ask_use_case
from tests.integration.conftest import auth
from tests.integration.job_worker import handlers
from tests.integration.test_job_queue import Environment, migrate
from tests.integration.test_jobs_api import JobAPI
from tests.integration.test_jobs_api import api as api
from tests.support.knowledge_fakes import harness, hit

NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture(scope="module")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def jobs() -> Iterator[Environment]:
    """A unique schema per test; drop it intact, including immutable retry audit."""
    source = os.environ.get("T20_TEST_DATABASE_URL")
    broker = os.environ.get("T20_TEST_REDIS_URL")
    if not source or not broker:
        if os.environ.get("CI"):
            pytest.fail("PostgreSQL and Redis test URLs are required")
        pytest.skip("Set T20_TEST_DATABASE_URL and T20_TEST_REDIS_URL")
    engine = sa.create_engine(source)
    schema = "ticket24_" + uuid4().hex
    queue = schema
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(sa.schema.CreateSchema(schema))
    url = (
        sa.engine.make_url(source)
        .update_query_dict({"options": f"-csearch_path={schema},public"})
        .render_as_string(hide_password=False)
    )
    runtime = None
    try:
        migrate(url)
        runtime = build_job_runtime(
            Settings(
                _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option.
                database=DatabaseSettings(url=url),
                queue=QueueSettings(broker_url=broker, default_queue=queue),
            ),
            handlers=handlers(url),
        )
        yield Environment(runtime, uuid4(), url, broker, queue)
    finally:
        if runtime is not None:
            asyncio.run(runtime.close())
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


def create_session(api: JobAPI) -> UUID:
    response = api.client.post(
        "/api/v1/sessions", json={"title": "Synthetic test"}, headers=auth(api.tokens[Role.ANALYST])
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def history(api: JobAPI, identifier: UUID):
    return api.client.get(
        f"/api/v1/sessions/{identifier}/messages", headers=auth(api.tokens[Role.ANALYST])
    )


@pytest.mark.parametrize("refused", [False, True])
async def test_http_history_survives_new_pool_and_keeps_exact_citations(
    api: JobAPI, jobs: Environment, refused: bool
) -> None:
    identifier = create_session(api)
    qa = harness([] if refused else [hit(1)])
    assert isinstance(api.client.app, FastAPI)
    api.client.app.dependency_overrides[get_ask_use_case] = lambda: qa.ask
    response = api.client.post(
        "/api/v1/ask",
        json={"question": "opening hours", "session_id": str(identifier)},
        headers=auth(api.tokens[Role.ANALYST]),
    )
    assert response.status_code == 200, response.text
    messages = history(api, identifier).json()["items"]
    assert messages[1]["answer"] == response.json()
    assert messages[1]["answer"]["refused"] is refused
    database = Database(jobs.url, pooling=False)
    try:
        read = await PostgresSessionStore(database.session_factory).messages(
            identifier, api.owners[Role.ANALYST], 100, 0
        )
        assert [item.role for item in read] == ["user", "assistant"]
        assert read[1].answer is not None and read[1].answer.refused is refused
        assert read[1].answer.answer == response.json()["answer"]
        assert len(read[1].answer.citations) == (0 if refused else 1)
    finally:
        await database.dispose()
    for role in (Role.REVIEWER, Role.ADMIN):
        denied = api.client.get(
            f"/api/v1/sessions/{identifier}/messages?owner_id={api.owners[role]}",
            headers={**auth(api.tokens[role]), "X-Role": "admin"},
        )
        assert denied.status_code == 403
        assert (
            api.client.get("/api/v1/sessions", headers=auth(api.tokens[role])).json()["items"] == []
        )


async def test_concurrent_appends_commit_complete_ordered_pairs(
    api: JobAPI, jobs: Environment
) -> None:
    identifier = create_session(api)
    database = Database(jobs.url, pooling=False)
    try:
        store = PostgresSessionStore(database.session_factory)
        answer = AskResult("Not enough information in the corpus", (), True, str(uuid4()))
        await asyncio.gather(
            *[
                store.append_exchange(
                    identifier, api.owners[Role.ANALYST], f"question {i}", answer, NOW
                )
                for i in range(8)
            ]
        )
        messages = await store.messages(identifier, api.owners[Role.ANALYST], 100, 0)
        assert [message.sequence for message in messages] == list(range(1, 17))
        assert [message.role for message in messages] == ["user", "assistant"] * 8
        assert len({message.content for message in messages if message.role == "user"}) == 8
        assert len(await store.messages(identifier, api.owners[Role.ANALYST], 1, 16)) == 0
    finally:
        await database.dispose()


async def test_failed_assistant_insert_rolls_back_question(api: JobAPI, jobs: Environment) -> None:
    identifier = create_session(api)
    database = Database(jobs.url, pooling=False)
    # A DB-enforced failure on the second row proves the transaction boundary.
    with jobs.runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "ALTER TABLE session_messages ADD CONSTRAINT ticket24_reject_assistant "
                "CHECK (role <> 'assistant') NOT VALID"
            )
        )
    try:
        store = PostgresSessionStore(database.session_factory)
        with pytest.raises(HistoryStoreUnavailableError):
            await store.append_exchange(
                identifier,
                api.owners[Role.ANALYST],
                "question",
                AskResult("Not enough information in the corpus", (), True, str(uuid4())),
                NOW,
            )
        assert await store.messages(identifier, api.owners[Role.ANALYST], 50, 0) == []
    finally:
        with jobs.runtime.engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE session_messages DROP CONSTRAINT ticket24_reject_assistant")
            )
        await database.dispose()


async def test_job_retry_api_preserves_identity_and_writes_one_audit(
    api: JobAPI, jobs: Environment
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=api.owners[Role.ANALYST])
    await jobs.runtime.service.store.transition(job.id, JobState.STARTED, NOW)
    await jobs.runtime.service.store.checkpoint(job.id, {"saved": {"value": 1}}, NOW)
    await jobs.runtime.service.store.transition(
        job.id, JobState.FAILED, NOW, error="JOB_EXECUTION_FAILED"
    )
    before = await jobs.runtime.service.store.get(job.id)
    url = f"/api/v1/jobs/{job.id}/retry"
    for role in (Role.ANALYST, Role.REVIEWER):
        denied = api.client.post(url, json={"reason": "Try again"}, headers=auth(api.tokens[role]))
        assert denied.status_code == 403
    responses = await asyncio.gather(
        *[
            asyncio.to_thread(
                api.client.post,
                url,
                json={"reason": "Authorized operator retry"},
                headers=auth(api.tokens[Role.ADMIN]),
            )
            for _ in range(2)
        ]
    )
    assert sorted(response.status_code for response in responses) == [202, 409]
    after = await jobs.runtime.service.store.get(job.id)
    assert after is not None and after.state == JobState.QUEUED
    assert after.checkpoint_data == {"saved": {"value": 1}}
    assert before is not None and after.idempotency_key == before.idempotency_key
    with jobs.runtime.engine.connect() as connection:
        events = Base.metadata.tables["job_events"]
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(events)
                .where(events.c.job_id == job.id, events.c.event_type == "job.manual_retry")
            )
            == 1
        )


async def test_retry_cannot_bypass_awaiting_approval(api: JobAPI, jobs: Environment) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=api.owners[Role.ANALYST])
    await jobs.runtime.service.store.transition(job.id, JobState.STARTED, NOW)
    await jobs.runtime.service.store.transition(
        job.id, JobState.FAILED, NOW, error="JOB_EXECUTION_FAILED"
    )
    workflows = Base.metadata.tables["workflow_runs"]
    with jobs.runtime.engine.begin() as connection:
        connection.execute(
            workflows.insert().values(
                id=uuid4(),
                user_id=api.owners[Role.ANALYST],
                correlation_id=str(uuid4()),
                case_summary="synthetic",
                state="AWAITING_APPROVAL",
                approval_job_id=job.id,
            )
        )
    response = api.client.post(
        f"/api/v1/jobs/{job.id}/retry",
        json={"reason": "Try to bypass review"},
        headers=auth(api.tokens[Role.ADMIN]),
    )
    assert response.status_code == 409
    assert (await jobs.runtime.service.get(job.id)).state == JobState.FAILED


def test_history_migration_preserves_sessions_and_rejects_destructive_downgrade(
    api: JobAPI, jobs: Environment
) -> None:
    identifier = create_session(api)
    # Empty history can be downgraded and recreated without dropping existing sessions.
    migrate(jobs.url, "c22a4b8f901d", downgrade=True)
    migrate(jobs.url)
    migrate(jobs.url)
    assert history(api, identifier).json()["items"] == []
    qa = harness([])
    assert isinstance(api.client.app, FastAPI)
    api.client.app.dependency_overrides[get_ask_use_case] = lambda: qa.ask
    answer = api.client.post(
        "/api/v1/ask",
        json={"question": "missing", "session_id": str(identifier), "stream": True},
        headers=auth(api.tokens[Role.ANALYST]),
    )
    assert answer.status_code == 200 and "event: refusal" in answer.text
    with pytest.raises(RuntimeError, match="Cannot discard persisted session history"):
        migrate(jobs.url, "c22a4b8f901d", downgrade=True)
    assert len(history(api, identifier).json()["items"]) == 2


async def test_postgres_job_list_filters_before_limiting(api: JobAPI, jobs: Environment) -> None:
    own = await jobs.runtime.service.submit("diagnostic", {}, user_id=api.owners[Role.ANALYST])
    await jobs.runtime.service.submit("diagnostic", {}, user_id=api.owners[Role.REVIEWER])
    response = api.client.get(
        "/api/v1/jobs?state=QUEUED&limit=1", headers=auth(api.tokens[Role.ANALYST])
    )
    assert response.status_code == 200
    assert [item["job_id"] for item in response.json()["items"]] == [str(own.id)]
    assert (
        api.client.get("/api/v1/jobs?state=FAILED", headers=auth(api.tokens[Role.ANALYST])).json()[
            "items"
        ]
        == []
    )
    all_jobs = api.client.get("/api/v1/jobs", headers=auth(api.tokens[Role.ADMIN])).json()
    assert len(all_jobs["items"]) == 2
