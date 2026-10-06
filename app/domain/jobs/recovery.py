"""Recovery rules over the existing job lifecycle and persisted workflow phase."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError

# These are phase projections, not a clinical workflow state machine. #17 owns
# transitions. Unknown phases fail closed until explicitly supported.
EXECUTING_PHASES = frozenset({"RESEARCH", "SAFETY_CHECK", "DRAFT", "APPROVED", "FINALIZE"})


def permits_execution(phase: str | None) -> bool:
    return phase is None or phase in EXECUTING_PHASES


@dataclass(frozen=True, slots=True)
class JobRetryPolicy:
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0

    def delay(self, attempt_number: int) -> timedelta:
        # Multiplication is capped each time, including after many manual retries.
        seconds = min(self.backoff_base_seconds, self.max_backoff_seconds)
        for _ in range(max(0, attempt_number - 1)):
            if seconds >= self.max_backoff_seconds:
                break
            seconds = min(seconds * self.backoff_multiplier, self.max_backoff_seconds)
        return timedelta(seconds=seconds)


def retry_job(
    job: Job, now: datetime, *, due: datetime, error: str, manual: bool = False
) -> Job:
    required = JobState.FAILED if manual else JobState.STARTED
    if job.state != required or job.cancellation_requested:
        raise InvalidStateTransitionError("This job cannot be retried.")
    if not manual and job.attempt_number >= job.max_attempts:
        return job.transition(JobState.FAILED, now, error="JOB_RETRIES_EXHAUSTED")
    return replace(
        job,
        state=JobState.QUEUED,
        updated_at=now,
        completed_at=None,
        result_payload=None,
        last_error=error,
        next_retry_at=due,
        # Manual retry grants one execution when the automatic budget is spent.
        # It never resets accounting or grants another automatic retry budget.
        max_attempts=max(job.max_attempts, job.attempt_number + 1) if manual else job.max_attempts,
        lease_owner=None,
        lease_acquired_at=None,
        lease_expires_at=None,
        paused_at=None,
        last_dispatched_at=None,
    )
