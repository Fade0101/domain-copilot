"""Celery + Redis broker, carrying only durable PostgreSQL job IDs (T7-01)."""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit
from uuid import UUID

from celery import Celery
from kombu.exceptions import OperationalError

from app.application.errors import ConfigurationError, JobQueueUnavailableError
from app.application.jobs.runner import JobRunner
from app.application.ports.queue import IJobQueue

TASK_NAME = "domain_copilot.execute_job"


def create_celery_app(
    broker_url: str,
    queue_name: str,
    *,
    visibility_timeout: int,
    connection_timeout: float,
) -> Celery:
    app = Celery("domain_copilot", broker=broker_url)
    # Celery resolves broker_url as os.environ["CELERY_BROKER_URL"] or the
    # configured value, and the environment wins. Unlike CELERY_RESULT_BACKEND
    # below, this cannot be neutralised from here -- Settings.broker_url is a
    # property that reads the environment on every access. An inherited value
    # would be silent and bad in a specific way: the API would publish job ids to
    # a broker the worker never consumes, so jobs sit QUEUED, reconcile()
    # republishes to the same wrong broker, and settings.queue.broker_url still
    # reports the configured value. Refuse to start instead.
    if str(app.conf.broker_url or "").strip() != broker_url.strip():
        raise ConfigurationError(
            "CELERY_BROKER_URL is set in the environment and overrides "
            "QUEUE__BROKER_URL. Unset it and configure the broker through "
            "QUEUE__BROKER_URL only."
        )
    try:
        if urlsplit(app.conf.broker_url).scheme not in {"redis", "rediss"}:
            raise ValueError("unsupported transport")
    except ValueError:
        raise ConfigurationError(
            "Jobs require a Redis broker URL (redis:// or rediss://)."
        ) from None
    # Even CELERY_RESULT_BACKEND inherited from the environment cannot make a
    # transport result authoritative or create an extra result store.
    app.backend_cls = "disabled"
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_default_queue=queue_name,
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=False,
        worker_prefetch_multiplier=1,
        task_publish_retry=False,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=connection_timeout,
        broker_transport_options={
            "visibility_timeout": visibility_timeout,
            "socket_connect_timeout": connection_timeout,
            "socket_timeout": connection_timeout,
            "max_retries": 0,
        },
        worker_hijack_root_logger=False,
        enable_utc=True,
    )
    return app


def register_job_task(app: Celery, runner: JobRunner) -> None:
    @app.task(name=TASK_NAME, ignore_result=True, shared=False, lazy=False)
    def execute_job(job_id: str) -> None:
        # Async persistence (including trace writes) uses psycopg on some deployments.
        # Its Windows sockets require a selector loop, scoped to this task only.
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=loop_factory) as runtime:
            runtime.run(runner.run(UUID(job_id)))


class CeleryJobQueue(IJobQueue):
    def __init__(self, app: Celery) -> None:
        self._app = app

    async def enqueue(self, job_id: UUID) -> None:
        try:
            await asyncio.to_thread(
                self._app.send_task,
                TASK_NAME,
                args=[str(job_id)],
                task_id=str(job_id),
                ignore_result=True,
                retry=False,
            )
        except (OperationalError, OSError):
            raise JobQueueUnavailableError("Job broker is unavailable.") from None
