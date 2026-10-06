"""Transactional approval persistence; no orchestrator or broker dependency."""

from typing import Protocol
from uuid import UUID

from app.application.approvals.contracts import (
    ApprovalCommand,
    DecisionResult,
    DraftReview,
    ReviewRecord,
)
from app.domain.auth.value_objects import UserId


class IApprovalStore(Protocol):
    async def prepare_review(
        self, snapshot: DraftReview, job_id: UUID, actor_id: UserId
    ) -> ReviewRecord:
        """Persist an immutable #16/#15 snapshot on an existing awaiting workflow.

        Verify current actor, workflow ownership, STARTED job and equal job/run
        owners. No public API accepts this snapshot. Exact registration replays
        reuse it; a different snapshot cannot replace what a reviewer has seen.
        """
        ...

    async def get(self, workflow_id: UUID, actor_id: UserId) -> ReviewRecord:
        """Read committed snapshot/decision/signal under fresh run authorization."""
        ...

    async def decide(self, command: ApprovalCommand, actor_id: UserId) -> DecisionResult:
        """Atomically reauthorize, lock, decide, audit and persist the handoff.

        Share #20's execution lock; refuse an executing/cancelled/terminal job.
        Lock workflow, approval and job records; lock the current actor's role.
        Reject completes the job using its domain lifecycle. Return only AFTER
        commit. Exact replays return the immutable decision without another event.
        """
        ...
