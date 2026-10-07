"""Source upload to searchable chunks on real PostgreSQL, Redis and Celery workers.

Uses a unique disposable database/queues and fails in CI when services are absent.
The embedding provider is deterministic/offline; all ingestion stages and stores
are the production implementations. A real model is exercised by Docker seed smoke.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.core.config import (
    DatabaseSettings,
    EmbeddingSettings,
    IngestionSettings,
    QueueSettings,
    Settings,
)
from app.core.container import get_container
from app.domain.auth.value_objects import Role
from app.domain.jobs.entities import JobState
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore
from tests.integration.conftest import auth
from tests.integration.ingestion_worker import build_test_runtime
from tests.integration.test_job_queue import Environment, migrate, wait_for, worker
from tests.integration.test_jobs_api import JobAPI
from tests.integration.test_jobs_api import api as api
from tests.support.document_fixtures import synthetic_pdf


@pytest.fixture(scope="module")
def ingestion_database() -> Iterator[str]:
    source = os.environ.get("TEST_DATABASE_URL")
    if not source or not os.environ.get("T20_TEST_REDIS_URL"):
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL and T20_TEST_REDIS_URL for ingestion")
        pytest.skip("Set TEST_DATABASE_URL and T20_TEST_REDIS_URL for real ingestion tests")
    name = "t8_ingestion_" + uuid4().hex
    admin_url = make_url(source).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    try:
        migrate(url)
        database = sa.create_engine(url, hide_parameters=True)
        with database.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE t8_embedding_calls "
                    "(digest TEXT PRIMARY KEY, calls INTEGER NOT NULL)"
                )
            )
        database.dispose()
        # asyncpg and psycopg both reach the same database. A plain URL lets the
        # production Database choose its own async driver.
        yield admin_url.set(database=name, drivername="postgresql").render_as_string(
            hide_password=False
        )
    finally:
        with engine.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()


@pytest.fixture
def jobs(ingestion_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Environment]:
    broker = os.environ["T20_TEST_REDIS_URL"]
    queue = "t8_ingestion_" + uuid4().hex
    for key, value in {
        "INGESTION__CHUNK_TOKENS": "16",
        "INGESTION__CHUNK_OVERLAP": "4",
        "EMBEDDING__BATCH_SIZE": "1",
        "EMBEDDING__MODEL": "test-model",
        "EMBEDDING__DIMENSIONS": "384",
        "EMBEDDING__VERSION": "t8-test-v1",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=ingestion_database),
        queue=QueueSettings(broker_url=broker, default_queue=queue, publish_timeout_seconds=0.5),
        embedding=EmbeddingSettings(
            model="test-model", dimensions=384, version="t8-test-v1", batch_size=1
        ),
        ingestion=IngestionSettings(chunk_tokens=16, chunk_overlap=4),
    )
    runtime = build_test_runtime(settings)
    owner = uuid4()
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users(id,email,hashed_password,role) VALUES (:id,:email,'','admin')"
            ),
            {"id": owner, "email": f"{owner}@example.com"},
        )
    try:
        yield Environment(runtime, owner, ingestion_database, broker, queue)
    finally:
        # Only this module's uniquely named scratch database is touched. Remove
        # seeded identities as well, since the HTTP fixture generates a new demo
        # password for each test and normal seeding never overwrites a password.
        with runtime.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM documents"))
            connection.execute(sa.text("DELETE FROM traces"))
            connection.execute(sa.text("DELETE FROM jobs"))
            connection.execute(sa.text("DELETE FROM users"))
            connection.execute(sa.text("DELETE FROM t8_embedding_calls"))
        with redis.Redis.from_url(broker) as client:
            client.delete(queue, "_kombu.binding." + queue)
        asyncio.run(runtime.close())


def upload(
    api: JobAPI, source: bytes, filename: str = "synthetic.md", version: int = 1
) -> httpx.Response:
    return api.client.post(
        "/api/v1/documents/ingest",
        params={"filename": filename, "version": version},
        content=source,
        headers={
            **auth(api.tokens[Role.ADMIN]),
            "Content-Type": "application/pdf" if filename.endswith(".pdf") else "text/markdown",
        },
    )


@pytest.mark.parametrize(
    "filename,source",
    [
        (
            "synthetic.md",
            b"# Synthetic guide\n\nAlpha evidence for a fictional example.\n\n"
            b"## Follow up\n\nBeta review notes.",
        ),
        (
            "synthetic.pdf",
            synthetic_pdf(
                ["OVERVIEW\nSynthetic alpha evidence.", "FOLLOW UP\nSynthetic beta review notes."]
            ),
        ),
    ],
    ids=["markdown", "pdf"],
)
async def test_upload_is_searchable_with_exact_citation_metadata(
    api: JobAPI,
    jobs: Environment,
    tmp_path: Path,
    filename: str,
    source: bytes,
) -> None:
    with worker(jobs, tmp_path, module="tests.integration.ingestion_worker"):
        accepted = upload(api, source, filename, version=7)
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert accepted.headers["location"] == body["status_url"]
        finished = await wait_for(jobs, UUID(body["job_id"]), JobState.COMPLETED)
    status = api.client.get(body["document_url"], headers=auth(api.tokens[Role.ADMIN]))
    assert status.status_code == 200, status.text
    document = status.json()
    assert document["status"] == "completed" and document["version"] == 7
    assert {item["status"] for item in document["stages"].values()} == {"completed"}
    assert document["ingested_at"] and document["chunk_count"] > 0
    assert document["error_stage"] is None and document["error_message"] is None
    assert finished.input_payload == {"document_id": body["document_id"]}
    assert all("artifact" in data or "status" in data for data in finished.checkpoint_data.values())
    assert jobs.runtime.database is not None
    retrieval = PostgresRetrievalStore(
        jobs.runtime.database.session_factory,
        embedding_model="test-model",
        embedding_dim=384,
        embedding_version="t8-test-v1",
    )
    hits = await retrieval.keyword_search("alpha", top_k=100)
    hit = next(hit for hit in hits if str(hit.document_id) == body["document_id"])
    assert hit.document_name == filename and hit.document_version == 7
    assert hit.ingested_at and hit.section
    assert hit.page == (1 if filename.endswith(".pdf") else None)
    query = [0.0] * 384
    query[1] = 1.0
    dense = await retrieval.dense_search(query, top_k=100)
    matched = next(hit for hit in dense if str(hit.document_id) == body["document_id"])
    assert matched.embedding_model == "test-model" and matched.embedding_version == "t8-test-v1"


async def test_concurrent_duplicate_uploads_commit_one_job_and_source(
    api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    source = ("# Concurrent\n\nSynthetic alpha " + str(uuid4())).encode()
    responses = await asyncio.gather(*(asyncio.to_thread(upload, api, source) for _ in range(4)))
    assert all(response.status_code == 202 for response in responses)
    ids = {response.json()["job_id"] for response in responses}
    assert len(ids) == 1
    assert sum(not response.json()["reused"] for response in responses) == 1
    document_id = responses[0].json()["document_id"]
    with jobs.runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM document_sources WHERE document_id=:id"),
                {"id": UUID(document_id)},
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM jobs WHERE input_payload->>'document_id'=:id"),
                {"id": document_id},
            )
            == 1
        )
    with worker(jobs, tmp_path, module="tests.integration.ingestion_worker"):
        await wait_for(jobs, UUID(next(iter(ids))), JobState.COMPLETED)
    repeated = upload(api, source).json()
    assert repeated["job_id"] in ids and repeated["state"] == "COMPLETED"


@pytest.mark.parametrize(
    "filename,source,reason",
    [
        ("broken.pdf", b"%PDF-1.4\ninvalid synthetic bytes", "could not be read"),
        ("broken.md", b"\xff\xfe invalid synthetic bytes", "UTF-8"),
    ],
)
async def test_failed_documents_are_queryable_with_the_stage_and_reason(
    api: JobAPI, jobs: Environment, tmp_path: Path, filename: str, source: bytes, reason: str
) -> None:
    with worker(jobs, tmp_path, module="tests.integration.ingestion_worker"):
        accepted = upload(api, source, filename).json()
        await wait_for(jobs, UUID(accepted["job_id"]), JobState.FAILED)
    document = api.client.get(accepted["document_url"], headers=auth(api.tokens[Role.ADMIN])).json()
    assert document["status"] == "failed" and document["error_stage"] == "extract"
    assert reason in document["error_message"]
    assert document["stages"]["clean"]["status"] == "pending"


async def test_redis_loss_does_not_lose_the_source_or_job(
    api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    source = ("# Redis recovery\n\nSynthetic beta " + str(uuid4())).encode()
    accepted = upload(api, source).json()
    with redis.Redis.from_url(jobs.broker) as client:
        assert client.delete(jobs.queue) == 1
    assert jobs.runtime.ingestion is not None
    assert await jobs.runtime.ingestion.store.source(UUID(accepted["document_id"])) == source
    assert await jobs.runtime.service.reconcile() >= 1
    with worker(jobs, tmp_path, module="tests.integration.ingestion_worker"):
        await wait_for(jobs, UUID(accepted["job_id"]), JobState.COMPLETED)


async def test_worker_restart_reuses_committed_embedding_batches(
    api: JobAPI, jobs: Environment, tmp_path: Path
) -> None:
    source = ("# Restart\n\n" + " ".join(f"restartword{index}" for index in range(50))).encode()
    with worker(
        jobs,
        tmp_path,
        module="tests.integration.ingestion_worker",
        environment_overrides={"T8_TEST_PAUSE_AFTER_ARTIFACT": "embed:0"},
    ) as first:
        accepted = upload(api, source).json()
        document_id, job_id = UUID(accepted["document_id"]), UUID(accepted["job_id"])
        assert jobs.runtime.ingestion is not None
        deadline = time.monotonic() + 20
        while await jobs.runtime.ingestion.store.artifact(document_id, "embed:0") is None:
            assert time.monotonic() < deadline, "First embedding artifact did not commit"
            await asyncio.sleep(0.1)
        checkpointed = (await jobs.runtime.service.get(job_id)).checkpoint_data
        assert "ingest:chunk" in checkpointed and "ingest:embed:0" not in checkpointed
        first.kill()
        first.wait(timeout=10)
    with jobs.runtime.engine.connect() as connection:
        before: dict[str, int] = dict(
            connection.execute(sa.text("SELECT digest,calls FROM t8_embedding_calls")).all()
        )
    await jobs.runtime.service.resume(job_id)
    with worker(jobs, tmp_path, module="tests.integration.ingestion_worker"):
        completed = await wait_for(jobs, job_id, JobState.COMPLETED)
        # A second task on the same process also exercises async DB lifetime across
        # the separate asyncio.run loops used by the real Celery task adapter.
        another = upload(api, b"# Another\n\nSynthetic gamma text after restart.").json()
        await wait_for(jobs, UUID(another["job_id"]), JobState.COMPLETED)
    with jobs.runtime.engine.connect() as connection:
        after: dict[str, int] = dict(
            connection.execute(sa.text("SELECT digest,calls FROM t8_embedding_calls")).all()
        )
        rows = connection.scalar(
            sa.text("SELECT count(*) FROM chunks WHERE document_id=:id"), {"id": document_id}
        )
    assert all(after[digest] == calls for digest, calls in before.items())
    assert completed.result_payload is not None
    assert rows == completed.result_payload["chunk_count"]


def test_ingestion_authorization_and_document_ownership(api: JobAPI) -> None:
    accepted = upload(api, b"# Ownership\n\nSynthetic content.").json()
    for role in (Role.ANALYST, Role.REVIEWER):
        headers = {**auth(api.tokens[role]), "Content-Type": "text/markdown"}
        assert (
            api.client.post(
                "/api/v1/documents/ingest?filename=source.md", content=b"alpha", headers=headers
            ).status_code
            == 403
        )
        assert api.client.get(accepted["document_url"], headers=headers).status_code == 403
    assert (
        api.client.post("/api/v1/documents/ingest?filename=source.md", content=b"alpha").status_code
        == 401
    )
    assert api.client.get(accepted["document_url"]).status_code == 401


@pytest.mark.parametrize("chunked", [False, True])
def test_upload_limit_rejects_declared_and_streamed_oversize_before_storage(
    api: JobAPI, jobs: Environment, chunked: bool
) -> None:
    get_container().ingestion_service.max_upload_bytes = 8
    with jobs.runtime.engine.connect() as connection:
        before = connection.scalar(sa.text("SELECT count(*) FROM documents"))
    content = iter([b"alpha", b"beta"]) if chunked else b"alphabeta"
    response = api.client.post(
        "/api/v1/documents/ingest?filename=source.md",
        content=content,
        headers={**auth(api.tokens[Role.ADMIN]), "Content-Type": "text/markdown"},
    )
    assert response.status_code == 413 and response.json()["code"] == "UPLOAD_TOO_LARGE"
    with jobs.runtime.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM documents")) == before


def test_migration_replay_preserves_preexisting_documents_and_vectors(
    ingestion_database: str,
) -> None:
    """The T8 revision adds storage without rebuilding or losing T9's index data."""
    url = (
        make_url(ingestion_database)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )
    migrate(url, "4c1e9a7d52b8", downgrade=True)
    engine = sa.create_engine(url, hide_parameters=True)
    owner, document_id, chunk_id, embedding_id = (uuid4() for _ in range(4))
    vector = "[1," + ",".join("0" for _ in range(383)) + "]"
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users(id,email,hashed_password,role) "
                    "VALUES (:id,:email,'','admin')"
                ),
                {"id": owner, "email": f"{owner}@example.com"},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO documents(id,user_id,filename,content,status,metadata) "
                    "VALUES (:id,:owner,'legacy.md','Synthetic legacy content','COMPLETED','{}')"
                ),
                {"id": document_id, "owner": owner},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO chunks(id,document_id,text,metadata,section,page,token_count) "
                    "VALUES (:id,:document,'Synthetic legacy alpha','{}','Legacy',1,3)"
                ),
                {"id": chunk_id, "document": document_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO chunk_embeddings"
                    "(id,chunk_id,embedding,embedding_model,embedding_dim,embedding_version) "
                    "VALUES (:id,:chunk,CAST(:vector AS vector),'legacy-model',384,'1')"
                ),
                {"id": embedding_id, "chunk": chunk_id, "vector": vector},
            )
        migrate(url)
        migrate(url)
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT content,version,content_hash FROM documents WHERE id=:id"),
                {"id": document_id},
            ).one()
            assert tuple(row) == ("Synthetic legacy content", 1, None)
            assert (
                connection.scalar(
                    sa.text("SELECT embedding::text FROM chunk_embeddings WHERE id=:id"),
                    {"id": embedding_id},
                )
                == vector
            )
            assert (
                connection.scalar(
                    sa.text("SELECT document_version FROM chunks WHERE id=:id"), {"id": chunk_id}
                )
                == 1
            )
        migrate(url, "4c1e9a7d52b8", downgrade=True)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM chunk_embeddings WHERE id=:id"),
                    {"id": embedding_id},
                )
                == 1
            )
        migrate(url)
    finally:
        engine.dispose()
