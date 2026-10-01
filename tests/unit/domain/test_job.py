"""The exact T7 lifecycle rejects shortcuts and keeps terminal states final."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def new_job() -> Job:
    return Job(UUID(int=1), "diagnostic", {}, UUID(int=2), NOW, NOW)


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED])
def test_lifecycle_records_start_and_terminal_timestamps(terminal: JobState) -> None:
    job = new_job().transition(JobState.QUEUED, NOW)
    assert job.attempt_number == 0
    started = NOW + timedelta(seconds=1)
    job = job.transition(JobState.STARTED, started)
    finished = NOW + timedelta(seconds=2)
    job = job.transition(terminal, finished, result={"ok": True}, error="JOB_HANDLER_FAILED")
    assert job.terminal
    assert job.started_at == started
    assert job.completed_at == finished
    assert job.attempt_number == 1
    assert job.result_payload == ({"ok": True} if terminal == JobState.COMPLETED else None)
    assert job.last_error == ("JOB_HANDLER_FAILED" if terminal == JobState.FAILED else None)


@pytest.mark.parametrize("target", [JobState.STARTED, JobState.COMPLETED, JobState.FAILED])
def test_pending_job_cannot_skip_queue(target: JobState) -> None:
    with pytest.raises(InvalidStateTransitionError):
        new_job().transition(target, NOW)


def test_queued_job_cannot_skip_execution() -> None:
    job = new_job().transition(JobState.QUEUED, NOW)
    with pytest.raises(InvalidStateTransitionError):
        job.transition(JobState.COMPLETED, NOW)


@pytest.mark.parametrize("terminal", [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED])
@pytest.mark.parametrize("target", list(JobState))
def test_terminal_job_cannot_be_reopened(terminal: JobState, target: JobState) -> None:
    job = new_job().transition(JobState.QUEUED, NOW).transition(JobState.STARTED, NOW)
    job = job.transition(terminal, NOW)
    with pytest.raises(InvalidStateTransitionError):
        job.transition(target, NOW)
