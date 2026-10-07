"""Real HTTP/PG/Redis/Celery browser harness with synthetic provider I/O only.

Creates and removes its own UUID-named schema/queue. Never truncates a shared
database or flushes Redis. Production does not import this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from app.application.auth.context import Principal
from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.core.config import AuthSettings, DatabaseSettings, LLMSettings, QueueSettings, Settings
from app.core.container import Container, build_job_runtime
from app.domain.auth.value_objects import EmailAddress
from app.domain.jobs.entities import Job, JobState
from app.infrastructure.persistence.job_store import job_insert_values
from app.infrastructure.persistence.models import JobModel, WorkflowRunModel
from app.presentation.api import dependencies, security
from app.presentation.api.app import create_app
from app.presentation.api.ask_stream import stream_answer
from app.presentation.api.routes import knowledge
from app.presentation.api.schemas.knowledge import AnswerResponse, RefusalResponse
from tests.integration.test_job_queue import migrate
from tests.support.approval_fixtures import review_snapshot
from tests.web.providers import BrowserAsk, BrowserClinicalHandler

FLAGGED_RUN = UUID("00000000-0000-4000-8000-000000000025")
PASSWORD = "synthetic-browser-password-25"


async def seed_flagged_review(container: Container) -> None:
    user = await container.user_repository.get_by_email(EmailAddress("analyst@example.com"))
    assert user is not None and container.database is not None
    snapshot = review_snapshot(FLAGGED_RUN, kind="flagged")
    now = container._clock.now()
    job = Job(
        uuid4(),
        "clinical.workflow",
        {},
        UUID(user.id.value),
        now,
        now,
        state=JobState.STARTED,
        attempt_number=1,
        started_at=now,
    )
    async with container.database.session_factory() as session, session.begin():
        session.add(JobModel(**job_insert_values(job)))
        session.add(
            WorkflowRunModel(
                id=FLAGGED_RUN,
                user_id=UUID(user.id.value),
                correlation_id=str(uuid4()),
                case_summary="Synthetic flagged review fixture",
                state="AWAITING_APPROVAL",
            )
        )
    await container.approval_service().prepare_review(snapshot, job.id, Principal.from_user(user))


async def paced_answer(result: AnswerResponse | RefusalResponse) -> AsyncIterator[str]:
    # Simulate packet latency so the browser can assert incremental delivery.
    # The frames and the committed answer come from the real endpoint/serializer.
    async for frame in stream_answer(result):
        yield frame
        await asyncio.sleep(0.06)


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    source = os.environ["T20_TEST_DATABASE_URL"]
    broker = os.environ["T20_TEST_REDIS_URL"]
    schema = "t25_web_" + uuid4().hex
    engine = sa.create_engine(source, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(sa.schema.CreateSchema(schema))
    url = (
        sa.engine.make_url(source)
        .update_query_dict({"options": f"-csearch_path={schema},public"})
        .render_as_string(hide_password=False)
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=url),
        queue=QueueSettings(broker_url=broker, default_queue=schema),
        llm=LLMSettings(provider="ollama", fallback=None),
        auth=AuthSettings(
            secret_key=SecretStr("synthetic-browser-signing-secret-25"),
            demo_password=SecretStr(PASSWORD),
            bcrypt_rounds=10,
        ),
    )
    patch = pytest.MonkeyPatch()
    try:
        migrate(url)
        container = Container(settings)
        container._jobs = build_job_runtime(
            settings,
            database=container.database,
            handlers=[DiagnosticJobHandler(), BrowserClinicalHandler(settings)],
        )
        patch.setattr(dependencies, "get_container", lambda: container)
        patch.setattr(security, "get_container", lambda: container)
        app = create_app(settings)
        app.dependency_overrides[dependencies.get_ask_use_case] = lambda: BrowserAsk(container)
        knowledge.stream_answer = paced_answer

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            await container.seed_demo_accounts()
            await seed_flagged_review(container)
            # SecretStr serialization masks the test signing key; the worker needs
            # the same deliberately synthetic value for the real auth path.
            worker_settings = settings.model_dump(mode="json")
            assert settings.auth.secret_key is not None
            worker_settings["auth"]["secret_key"] = settings.auth.secret_key.get_secret_value()
            artifacts = Path(".artifacts")
            artifacts.mkdir(exist_ok=True)
            worker_creationflags = 0
            if sys.platform == "win32":
                worker_creationflags = subprocess.CREATE_NO_WINDOW
            with (artifacts / "web-worker.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "tests.web.worker"],
                    env={**os.environ, "WEB_TEST_SETTINGS": json.dumps(worker_settings)},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=worker_creationflags,
                )
                try:
                    yield
                finally:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)
                    await container.dispose()

        app.router.lifespan_context = lifespan
        # Uvicorn.run selects a Proactor loop on recent Windows releases, which
        # psycopg explicitly cannot use. Keep the selector policy established above.
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=int(os.environ.get("WEB_TEST_PORT", "8765")))
        )
        asyncio.run(server.serve())
    finally:
        patch.undo()
        # Remove only this harness's UUID-named queue and schema.
        client = redis.Redis.from_url(broker)
        client.delete(schema, f"_kombu.binding.{schema}")
        client.close()
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


if __name__ == "__main__":
    main()
