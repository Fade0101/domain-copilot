"""Jobs have a lifecycle independent of clinical workflow state (BRD T7-09)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.shared.errors import InvalidStateTransitionError


class JobState(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TRANSITIONS = {
    JobState.PENDING: frozenset({JobState.QUEUED}),
    JobState.QUEUED: frozenset({JobState.STARTED}),
    JobState.STARTED: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    operation_type: str
    input_payload: dict[str, Any]
    user_id: UUID
    created_at: datetime
    updated_at: datetime
    state: JobState = JobState.PENDING
    correlation_id: UUID | None = None
    checkpoint_data: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt_number: int = 0
    cancellation_requested: bool = False

    @property
    def terminal(self) -> bool:
        return not _TRANSITIONS[self.state]

    def transition(
        self,
        target: JobState,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidStateTransitionError(f"Cannot move job from {self.state} to {target}.")
        return replace(
            self,
            state=target,
            updated_at=now,
            started_at=now if target == JobState.STARTED else self.started_at,
            completed_at=now if not _TRANSITIONS[target] else None,
            attempt_number=self.attempt_number + (target == JobState.STARTED),
            result_payload=result if target == JobState.COMPLETED else None,
            last_error=error if target == JobState.FAILED else None,
        )
