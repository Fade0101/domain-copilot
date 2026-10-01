"""Real PostgreSQL + Redis + separate Celery processes, including hard worker death.

Set T20_TEST_DATABASE_URL and T20_TEST_REDIS_URL to disposable local services.
Tests use a unique PostgreSQL schema and Redis queue, and never flush shared Redis.
CI must supply these services: missing configuration fails instead of silently skipping.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import DatabaseSettings, QueueSettings, Settings
from app.core.container import JobRuntime, build_job_runtime
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError
from tests.integration.job_worker import handlers

ROOT = Path(__file__).resolve().parents[2]


def migrate(url: str, revision: str = "head", *, downgrade: bool = False) -> None:
    engine = sa.create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "migrations"))
            config.attributes["connection"] = connection
            # Do not discover an existing public.alembic_version through search_path.
            config.attributes["version_table_schema"] = connection.scalar(
                sa.text("SELECT current_schema()")
            )
            (command.downgrade if downgrade else command.upgrade)(config, revision)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    source = os.environ.get("T20_TEST_DATABASE_URL")
    if not source or not os.environ.get("T20_TEST_REDIS_URL"):
        if os.environ.get("CI"):
            pytest.fail("CI must provide T20_TEST_DATABASE_URL and T20_TEST_REDIS_URL")
        pytest.skip("Set T20_TEST_DATABASE_URL and T20_TEST_REDIS_URL for real queue integration")
    engine = sa.create_engine(source, hide_parameters=True, connect_args={"connect_timeout": 5})
    schema = "t20_jobs_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(sa.schema.CreateSchema(schema))
    url = make_url(source).update_query_dict({"options": f"-csearch_path={schema},public"})
    rendered = url.render_as_string(hide_password=False)
    try:
        migrate(rendered)
        test_engine = sa.create_engine(rendered, hide_parameters=True)
        with test_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE job_test_effects (job_id UUID REFERENCES jobs(id) "
                    "ON DELETE CASCADE, "
                    "step TEXT, executions INTEGER NOT NULL, PRIMARY KEY(job_id, step))"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE job_test_release (job_id UUID PRIMARY KEY REFERENCES jobs(id) "
                    "ON DELETE CASCADE, allowed BOOLEAN NOT NULL)"
                )
            )
        test_engine.dispose()
        yield rendered
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


@dataclass
class Environment:
    runtime: JobRuntime
    owner: UUID
    url: str
    broker: str
    queue: str


@pytest.fixture
def jobs(database_url: str) -> Iterator[Environment]:
    broker = os.environ["T20_TEST_REDIS_URL"]
    queue = "t20_jobs_" + uuid4().hex
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option.
        database=DatabaseSettings(url=database_url),
        queue=QueueSettings(broker_url=broker, default_queue=queue, publish_timeout_seconds=0.5),
    )
    runtime = build_job_runtime(settings, handlers=handlers(database_url))
    owner = uuid4()
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users(id,email,hashed_password,role) VALUES (:id,:email,'','analyst')"
            ),
            {"id": owner, "email": f"{owner}@example.com"},
        )
    try:
        yield Environment(runtime, owner, database_url, broker, queue)
    finally:
        with runtime.engine.begin() as connection:
            # All rows are in this module's uniquely named, disposable schema.
            connection.execute(sa.text("DELETE FROM jobs"))
            connection.execute(sa.text("DELETE FROM users"))
        client = redis.Redis.from_url(broker)
        client.delete(queue, "_kombu.binding." + queue)
        client.close()
        asyncio.run(runtime.close())


@contextmanager
def worker(jobs: Environment, directory: Path) -> Iterator[subprocess.Popen[bytes]]:
    name = "t20-" + uuid4().hex + "@localhost"
    log_path = directory / (name.replace("@", "-") + ".log")
    environment = {
        **os.environ,
        "DATABASE__URL": jobs.url,
        "QUEUE__BROKER_URL": jobs.broker,
        "QUEUE__DEFAULT_QUEUE": jobs.queue,
        "QUEUE__PUBLISH_TIMEOUT_SECONDS": "0.5",
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.integration.job_worker",
                "--pool=solo",
                "--concurrency=1",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--loglevel=WARNING",
                "--hostname=" + name,
            ],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Worker exited: " + log_path.read_text(errors="replace")[-4000:])
                if jobs.runtime.celery_app.control.ping(destination=[name], timeout=0.5):
                    break
            else:
                pytest.fail("Worker was not ready: " + log_path.read_text(errors="replace")[-4000:])
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


async def wait_for(jobs: Environment, job_id: UUID, state: JobState) -> Job:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = await jobs.runtime.service.get(job_id)
        if job.state == state:
            return job
        if job.terminal:
            pytest.fail(f"Expected {state}, received {job.state} ({job.last_error})")
        await asyncio.sleep(0.1)
    pytest.fail(f"Timed out waiting for {job_id} to reach {state}")


async def test_real_worker_delivers_result_to_postgres(jobs: Environment, tmp_path: Path) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
        finished = await wait_for(jobs, job.id, JobState.COMPLETED)
    assert finished.result_payload == {"ok": True}
    assert finished.checkpoint_data == {"probe-v1": {"ok": True}}
    assert finished.attempt_number == 1
    assert jobs.runtime.celery_app.backend.__class__.__name__ == "DisabledBackend"


async def test_broker_message_contains_only_the_job_id(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    with redis.Redis.from_url(jobs.broker) as client:
        envelope = json.loads(client.lindex(jobs.queue, 0))
    args, kwargs, _ = json.loads(base64.b64decode(envelope["body"]))
    assert args == [str(job.id)] and kwargs == {}


async def test_broker_queue_loss_can_be_reconciled_from_postgres(
    jobs: Environment,
    tmp_path: Path,
) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    with redis.Redis.from_url(jobs.broker) as client:
        # Lose only this test's transport data; never flush somebody else's Redis.
        assert client.delete(jobs.queue) == 1
    assert (await jobs.runtime.service.get(job.id)).state == JobState.QUEUED
    assert await jobs.runtime.service.reconcile() == 1
    with worker(jobs, tmp_path):
        await wait_for(jobs, job.id, JobState.COMPLETED)


async def test_broker_outage_still_accepts_a_durable_job(jobs: Environment) -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))  # Reserved but not listening.
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option.
            database=DatabaseSettings(url=jobs.url),
            queue=QueueSettings(
                broker_url=f"redis://127.0.0.1:{unavailable.getsockname()[1]}/0",
                default_queue=jobs.queue,
                publish_timeout_seconds=0.1,
            ),
        )
        runtime = build_job_runtime(settings)
        try:
            job = await runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
            assert (await jobs.runtime.service.get(job.id)).state == JobState.QUEUED
        finally:
            await runtime.close()
    assert await jobs.runtime.service.reconcile() == 1


async def test_hard_worker_restart_skips_committed_steps(jobs: Environment, tmp_path: Path) -> None:
    with worker(jobs, tmp_path) as first_worker:
        job = await jobs.runtime.service.submit("test.checkpoint", {}, user_id=jobs.owner)
        await wait_for(jobs, job.id, JobState.STARTED)
        deadline = time.monotonic() + 15
        while "first" not in (await jobs.runtime.service.get(job.id)).checkpoint_data:
            assert time.monotonic() < deadline, "First checkpoint did not commit"
            await asyncio.sleep(0.1)
        first_worker.kill()
        first_worker.wait(timeout=10)
    with jobs.runtime.engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO job_test_release(job_id,allowed) VALUES (:id,true)"),
            {"id": job.id},
        )
    # Explicit PG recovery does not wait for Redis's visibility timeout.
    await jobs.runtime.service.resume(job.id)
    with worker(jobs, tmp_path):
        finished = await wait_for(jobs, job.id, JobState.COMPLETED)
        await asyncio.to_thread(
            jobs.runtime.celery_app.send_task,
            "domain_copilot.execute_job",
            args=[str(job.id)],
        )
        sentinel = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
        await wait_for(jobs, sentinel.id, JobState.COMPLETED)
    with jobs.runtime.engine.connect() as connection:
        effects: dict[str, int] = dict(
            connection.execute(
                sa.text("SELECT step,executions FROM job_test_effects WHERE job_id=:id"),
                {"id": job.id},
            ).all()
        )
    assert effects == {"first": 1, "second": 1}
    assert finished.result_payload == {"step": "second"}


async def test_real_handler_failure_has_a_safe_persisted_error(
    jobs: Environment,
    tmp_path: Path,
) -> None:
    with worker(jobs, tmp_path):
        job = await jobs.runtime.service.submit("test.failure", {}, user_id=jobs.owner)
        failed = await wait_for(jobs, job.id, JobState.FAILED)
    assert failed.last_error == "JOB_HANDLER_FAILED" and failed.result_payload is None


async def test_pause_releases_worker_without_recovery_replaying_it(
    jobs: Environment,
    tmp_path: Path,
) -> None:
    with worker(jobs, tmp_path):
        paused = await jobs.runtime.service.submit("test.pause", {}, user_id=jobs.owner)
        await wait_for(jobs, paused.id, JobState.STARTED)
        sentinel = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
        await wait_for(jobs, sentinel.id, JobState.COMPLETED)
        assert await jobs.runtime.service.reconcile() == 0
    assert (await jobs.runtime.service.get(paused.id)).state == JobState.STARTED


async def test_postgres_lock_excludes_another_connection(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    async with jobs.runtime.service.store.lock(job.id) as first:
        assert first is not None
        async with jobs.runtime.service.store.lock(job.id) as second:
            assert second is None
    async with jobs.runtime.service.store.lock(job.id) as third:
        assert third is not None


async def test_postgres_rejects_illegal_transition_and_checkpoint(jobs: Environment) -> None:
    job = await jobs.runtime.service.submit("diagnostic", {}, user_id=jobs.owner)
    with pytest.raises(InvalidStateTransitionError):
        await jobs.runtime.service.store.transition(job.id, JobState.COMPLETED, job.created_at)
    with pytest.raises(InvalidStateTransitionError):
        await jobs.runtime.service.store.checkpoint(job.id, {}, job.created_at)
    assert (await jobs.runtime.service.get(job.id)).state == JobState.QUEUED


def test_migrations_downgrade_and_reapply(database_url: str) -> None:
    migrate(database_url, "-1", downgrade=True)
    migrate(database_url)
    migrate(database_url)
    engine = sa.create_engine(database_url)
    try:
        columns = {column["name"] for column in sa.inspect(engine).get_columns("jobs")}
        assert {"input_payload", "checkpoint_data", "result_payload", "started_at"} <= columns
    finally:
        engine.dispose()
