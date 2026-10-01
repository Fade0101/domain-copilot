"""A real Celery worker with test-only handlers; never registered in the product."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import sqlalchemy as sa

from app.application.errors import JobPaused
from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.application.ports.jobs import IJobContext, IJobHandler
from app.core.config import get_settings
from app.core.container import build_job_runtime


class CheckpointProbe:
    operation_type = "test.checkpoint"

    def __init__(self, url: str) -> None:
        self.engine = sa.create_engine(url, hide_parameters=True)

    def validate(self, payload: dict[str, Any]) -> None:
        pass

    async def run(self, context: IJobContext) -> dict[str, Any]:
        async def record(step: str) -> dict[str, Any]:
            def write() -> None:
                with self.engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "INSERT INTO job_test_effects(job_id, step, executions) "
                            "VALUES (:id, :step, 1) ON CONFLICT(job_id, step) "
                            "DO UPDATE SET executions=job_test_effects.executions+1"
                        ),
                        {"id": context.job_id, "step": step},
                    )

            await asyncio.to_thread(write)
            return {"step": step}

        await context.step("first", lambda: record("first"))

        def released() -> bool:
            with self.engine.connect() as connection:
                return bool(
                    connection.scalar(
                        sa.text("SELECT allowed FROM job_test_release WHERE job_id=:id"),
                        {"id": context.job_id},
                    )
                )

        while not await asyncio.to_thread(released):
            await asyncio.sleep(0.1)
        return await context.step("second", lambda: record("second"))


class FailureProbe:
    operation_type = "test.failure"

    def validate(self, payload: dict[str, Any]) -> None:
        pass

    async def run(self, context: IJobContext) -> dict[str, Any]:
        raise RuntimeError("private handler diagnostic must not appear in the public job error")


class PauseProbe(DiagnosticJobHandler):
    operation_type = "test.pause"

    async def run(self, context: IJobContext) -> dict[str, Any]:
        await super().run(context)
        raise JobPaused()


def handlers(url: str) -> list[IJobHandler]:
    return [DiagnosticJobHandler(), CheckpointProbe(url), FailureProbe(), PauseProbe()]


if __name__ == "__main__":
    settings = get_settings()
    assert settings.database.url is not None
    runtime = build_job_runtime(settings, handlers=handlers(settings.database.url))
    runtime.celery_app.worker_main(["worker", *sys.argv[1:]])
