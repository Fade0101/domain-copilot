"""Evaluation-specific durability layered on the unmodified T7 lifecycle."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.application.auth.context import Principal
from app.application.errors import PermissionDeniedError
from app.domain.auth.value_objects import ResourceType, Role
from app.domain.jobs.entities import JobState
from tests.support.evaluation_fakes import USER, WorkerCrash, evaluation_harness, observation


async def test_all_cases_run_even_when_expectations_fail() -> None:
    h = evaluation_harness()
    h.probe.overrides["qa-one"] = observation(refused=True)
    job = await h.submit()
    await h.runner.run(job.id)
    completed = await h.jobs.get(job.id)
    assert completed.state == JobState.COMPLETED
    assert h.probe.calls == ["qa-one", "qa-two", "adv-one"]
    report = await h.service.report(job.id, Principal.from_user(USER))
    assert report["summary"]["executed_cases"] == 3
    assert report["summary"]["passed_cases"] == 2
    assert not report["summary"]["targets_met"]
    assert len(report["cases"]) == 3 and h.probe.closed == 1
    assert all(len(str(value)) < 2000 for value in completed.checkpoint_data.values())


async def test_crash_after_durable_result_before_checkpoint_never_repeats_that_case() -> None:
    h = evaluation_harness()
    h.artifacts.crash_after_write = "case/qa-one"
    job = await h.submit()
    with pytest.raises(WorkerCrash):
        await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.STARTED
    assert "evaluation/case/qa-one" not in (await h.jobs.get(job.id)).checkpoint_data
    await h.service.restart(job.id, Principal.from_user(USER))
    await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.COMPLETED
    assert h.probe.calls == ["qa-one", "qa-two", "adv-one"]


async def test_cancel_between_cases_and_restart_reuses_results_in_a_new_job() -> None:
    h = evaluation_harness()
    h.artifacts.cancel_after_write = "case/qa-one"
    job = await h.submit()
    await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.CANCELLED
    assert h.probe.calls == ["qa-one"]
    child = await h.service.restart(job.id, Principal.from_user(USER))
    assert child.id != job.id
    await h.runner.run(child.id)
    assert (await h.jobs.get(child.id)).state == JobState.COMPLETED
    assert (await h.jobs.get(job.id)).state == JobState.CANCELLED
    assert h.probe.calls == ["qa-one", "qa-two", "adv-one"]
    assert h.artifacts.values[(child.id, "case/qa-one")]["reused_from_job_id"] == str(job.id)


async def test_queued_cancel_settles_without_consuming_a_worker() -> None:
    h = evaluation_harness()
    job = await h.submit()
    await h.service.cancel(job.id, Principal.from_user(USER))
    await h.runner.run(job.id)
    assert h.store.transitions == [
        JobState.PENDING,
        JobState.QUEUED,
        JobState.CANCELLED,
    ]
    assert not h.probe.calls


async def test_cancellation_during_last_case_is_observed_before_completion() -> None:
    h = evaluation_harness()
    h.artifacts.cancel_after_write = "case/adv-one"
    job = await h.submit()
    await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.CANCELLED
    assert len(h.probe.calls) == 3


async def test_completed_restart_is_a_new_measurement_not_a_cached_report() -> None:
    h = evaluation_harness()
    job = await h.submit()
    await h.runner.run(job.id)
    child = await h.service.restart(job.id, Principal.from_user(USER))
    await h.runner.run(child.id)
    assert child.id != job.id and len(h.probe.calls) == 6


async def test_resume_rejects_changed_index_without_mixing_case_results() -> None:
    h = evaluation_harness()
    h.artifacts.crash_after_write = "case/qa-one"
    job = await h.submit()
    with pytest.raises(WorkerCrash):
        await h.runner.run(job.id)
    h.evidence.value = replace(h.evidence.value, fingerprint="changed-index")
    await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.FAILED
    assert h.probe.calls == ["qa-one"]
    assert h.artifacts.values[(job.id, "failure")]["code"] == "RESUME_PINS_CHANGED"


async def test_worker_reloads_role_after_queueing_and_refuses_demoted_admin() -> None:
    h = evaluation_harness()
    job = await h.submit()
    h.users.users[USER.id.value] = replace(USER, role=Role.ANALYST)
    await h.runner.run(job.id)
    assert (await h.jobs.get(job.id)).state == JobState.FAILED and not h.probe.calls


async def test_evaluation_submission_and_controls_require_existing_admin_permission() -> None:
    h = evaluation_harness()
    analyst = Principal.from_user(replace(USER, role=Role.ANALYST))
    with pytest.raises(PermissionDeniedError):
        await h.service.submit(analyst)
    job = await h.submit()
    h.owners.register(ResourceType.JOB, str(job.id), USER.id)
    for operation in (h.service.cancel, h.service.restart):
        with pytest.raises(PermissionDeniedError):
            await operation(job.id, analyst)
