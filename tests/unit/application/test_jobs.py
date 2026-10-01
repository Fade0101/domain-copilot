"""Submission durability and checkpoint resume, with no broker or database imports."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.errors import JobCancelled, JobPaused, JobStoreError
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.application.ports.jobs import IJobContext
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvariantViolationError
from tests.support.fakes import FixedClock, SequentialIdGenerator
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

NOW = datetime(2026, 10, 1, tzinfo=UTC)
OWNER = UUID(int=100)


class WorkerCrash(BaseException):
    """Abrupt worker death must not be mistaken for a handled domain failure."""


class StepHandler:
    operation_type = "steps"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.interrupt: BaseException | None = None

    def validate(self, payload: dict[str, Any]) -> None:
        pass

    async def run(self, context: IJobContext) -> dict[str, Any]:
        async def first() -> dict[str, Any]:
            self.calls.append("first")
            return {"value": 21}

        saved = await context.step("first-v1", first)
        if self.interrupt:
            error, self.interrupt = self.interrupt, None
            raise error

        async def second() -> dict[str, Any]:
            self.calls.append("second")
            return {"answer": saved["value"] * 2}

        return await context.step("second-v1", second)


def setup_jobs(
    handler: StepHandler, *, store: FakeJobStore | None = None
) -> tuple[
    FakeJobStore,
    FakeJobQueue,
    JobService,
    JobRunner,
]:
    store = store or FakeJobStore()
    queue = FakeJobQueue(store)
    registry = JobHandlerRegistry([handler])
    clock = FixedClock(NOW)
    service = JobService(store, queue, registry, clock, SequentialIdGenerator())
    return store, queue, service, JobRunner(store, registry, clock)


async def test_submission_is_durable_and_queued_before_publish() -> None:
    store, queue, service, _ = setup_jobs(StepHandler())
    job = await service.submit("steps", {"input": "original"}, user_id=OWNER)
    assert queue.ids == [job.id]
    assert store.transitions == [JobState.PENDING, JobState.QUEUED]
    assert (await service.get(job.id)).input_payload == {"input": "original"}


async def test_broker_failure_keeps_committed_job_for_reconciliation() -> None:
    store, queue, service, _ = setup_jobs(StepHandler())
    queue.unavailable = True
    job = await service.submit("steps", {}, user_id=OWNER)
    assert (await service.get(job.id)).state == JobState.QUEUED
    queue.unavailable = False
    assert await service.reconcile() == 1
    assert queue.ids == [job.id]
    assert len(store.jobs) == 1


@pytest.mark.parametrize("operation,payload", [("missing", {}), ("steps", {"x": "x" * 65_536})])
async def test_invalid_submission_has_no_persistence_or_queue_side_effects(
    operation: str,
    payload: dict[str, Any],
) -> None:
    store, queue, service, _ = setup_jobs(StepHandler())
    with pytest.raises(InvariantViolationError):
        await service.submit(operation, payload, user_id=OWNER)
    assert not store.jobs and not queue.ids


@pytest.mark.parametrize("payload", [{"value": (1, 2)}, {"nested": {1: "value"}}])
async def test_submission_rejects_types_that_change_after_json_roundtrip(
    payload: dict[str, Any],
) -> None:
    store, queue, service, _ = setup_jobs(StepHandler())
    with pytest.raises(InvariantViolationError):
        await service.submit("steps", payload, user_id=OWNER)
    assert not store.jobs and not queue.ids


async def test_new_runner_resumes_without_reexecuting_checkpointed_steps() -> None:
    handler = StepHandler()
    handler.interrupt = WorkerCrash()
    store, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    with pytest.raises(WorkerCrash):
        await runner.run(job.id)
    assert (await service.get(job.id)).checkpoint_data == {"first-v1": {"value": 21}}
    _, _, _, restarted_runner = setup_jobs(handler, store=store)
    await restarted_runner.run(job.id)
    finished = await service.get(job.id)
    assert handler.calls == ["first", "second"]
    assert finished.result_payload == {"answer": 42}
    assert finished.state == JobState.COMPLETED and finished.attempt_number == 1


async def test_completed_job_redelivery_does_not_repeat_work() -> None:
    handler = StepHandler()
    _, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    await runner.run(job.id)
    await runner.run(job.id)
    assert handler.calls == ["first", "second"]


async def test_handler_failure_records_safe_error_without_losing_checkpoints() -> None:
    handler = StepHandler()
    handler.interrupt = RuntimeError("sensitive input must not enter the job error")
    _, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    await runner.run(job.id)
    failed = await service.get(job.id)
    assert failed.state == JobState.FAILED
    assert failed.last_error == "JOB_HANDLER_FAILED"
    assert failed.checkpoint_data == {"first-v1": {"value": 21}}


async def test_storage_failure_leaves_job_started_for_resume() -> None:
    handler = StepHandler()
    handler.interrupt = JobStoreError("database unavailable")
    _, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    with pytest.raises(JobStoreError):
        await runner.run(job.id)
    assert (await service.get(job.id)).state == JobState.STARTED


async def test_deliberate_pause_is_not_selected_by_reconciliation() -> None:
    handler = StepHandler()
    handler.interrupt = JobPaused()
    store, queue, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    await runner.run(job.id)
    queue.ids.clear()
    assert await service.reconcile() == 0
    assert (await service.get(job.id)).state == JobState.STARTED
    assert job.id not in store.locked


@pytest.mark.parametrize("persisted_flag", [False, True])
async def test_cancellation_is_a_distinct_terminal_outcome(persisted_flag: bool) -> None:
    handler = StepHandler()
    handler.interrupt = JobCancelled()
    store, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    if persisted_flag:
        store.jobs[job.id] = replace(store.jobs[job.id], cancellation_requested=True)
    await runner.run(job.id)
    cancelled = await service.get(job.id)
    assert cancelled.state == JobState.CANCELLED and cancelled.last_error is None
    assert handler.calls == ([] if persisted_flag else ["first"])


async def test_simultaneous_delivery_cannot_execute_an_owned_job() -> None:
    handler = StepHandler()
    store, _, service, runner = setup_jobs(handler)
    job = await service.submit("steps", {}, user_id=OWNER)
    async with store.lock(job.id):
        await asyncio.gather(runner.run(job.id), runner.run(job.id))
        assert handler.calls == []
    await runner.run(job.id)
    assert handler.calls == ["first", "second"]
