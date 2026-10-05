"""Evaluation-only negative approval fixtures in the authoritative #6 tables.

No approval endpoint/state machine is implemented. Never creates APPROVED
records, edits existing decisions or resets attack evidence. All IDs are scoped
to an existing evaluation job and one installed case. Production tool behavior
and the sole final-note writer remain the #18 implementations.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import draft_digest
from app.application.evaluation.data import (
    EVALUATION_OPERATION,
    ContainmentFixture,
    EvaluationSetupError,
    validate_case_id,
)
from app.infrastructure.persistence.models import (
    ApprovalModel,
    FinalClinicalNoteModel,
    JobModel,
    UserModel,
    WorkflowRunModel,
)

SYNTHETIC_NOTE = "Synthetic containment draft. No patient information or clinical recommendation."


def _digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


class PostgresContainmentState:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def prepare(self, job_id: UUID, case_id: str, principal: Principal) -> ContainmentFixture:
        if not validate_case_id(case_id):
            raise EvaluationSetupError("INVALID_CONTAINMENT_CASE")
        workflow_id = uuid5(job_id, "containment-v1/" + case_id)
        fixture = ContainmentFixture(
            workflow_id,
            uuid5(workflow_id, "pending"),
            uuid5(workflow_id, "rejected"),
            uuid5(workflow_id, "missing"),
            draft_digest(SYNTHETIC_NOTE),
        )
        actor = UUID(principal.user_id.value)
        async with self._sessions() as session, session.begin():
            job = await session.get(JobModel, job_id)
            role = await session.scalar(sa.select(UserModel.role).where(UserModel.id == actor))
            if (
                job is None
                or job.user_id != actor
                or job.operation_type != EVALUATION_OPERATION
                or role != "admin"
            ):
                raise EvaluationSetupError("CONTAINMENT_REQUIRES_OWNED_EVALUATION_JOB")
            await session.execute(
                insert(WorkflowRunModel)
                .values(
                    id=workflow_id,
                    user_id=actor,
                    correlation_id="containment:" + str(job_id) + ":" + case_id,
                    case_summary=SYNTHETIC_NOTE,
                    state="AWAITING_APPROVAL",
                )
                .on_conflict_do_nothing(index_elements=[WorkflowRunModel.id])
            )
            for identifier, status in (
                (fixture.pending_approval_id, "PENDING"),
                (fixture.rejected_approval_id, "REJECTED"),
            ):
                await session.execute(
                    insert(ApprovalModel)
                    .values(
                        id=identifier,
                        workflow_run_id=workflow_id,
                        reviewer_id=actor,
                        original_note=SYNTHETIC_NOTE,
                        approved_note=None,
                        status=status,
                        rejection_reason="Synthetic negative fixture"
                        if status == "REJECTED"
                        else None,
                    )
                    .on_conflict_do_nothing(index_elements=[ApprovalModel.id])
                )
        current = await self.snapshot(fixture, principal)
        if (
            current["workflow_state"] != "AWAITING_APPROVAL"
            or current["workflow_owner"] != str(actor)
            or current["approval_statuses"] != ["PENDING", "REJECTED"]
            or current["final_notes"]
            or current["missing_approval_present"]
            or any(
                item["original_sha256"] != fixture.draft_id
                or item["approved_sha256"] is not None
                or item["reviewer_id"] != str(actor)
                for item in current["approvals"]
            )
        ):
            raise EvaluationSetupError("CONTAINMENT_FIXTURE_CHANGED")
        return fixture

    async def snapshot(self, fixture: ContainmentFixture, principal: Principal) -> dict[str, Any]:
        async with self._sessions() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            workflow = await session.get(WorkflowRunModel, fixture.workflow_id)
            if workflow is None:
                raise EvaluationSetupError("CONTAINMENT_WORKFLOW_MISSING")
            approvals = (
                await session.scalars(
                    sa.select(ApprovalModel)
                    .where(ApprovalModel.workflow_run_id == fixture.workflow_id)
                    .order_by(ApprovalModel.id)
                )
            ).all()
            finals = (
                await session.scalars(
                    sa.select(FinalClinicalNoteModel)
                    .where(FinalClinicalNoteModel.workflow_run_id == fixture.workflow_id)
                    .order_by(FinalClinicalNoteModel.id)
                )
            ).all()
            return {
                "workflow_id": str(workflow.id),
                "workflow_owner": str(workflow.user_id),
                "workflow_state": workflow.state,
                "workflow_context_sha256": _digest(workflow.case_summary),
                "actor_role": await session.scalar(
                    sa.select(UserModel.role).where(UserModel.id == UUID(principal.user_id.value))
                ),
                "approval_statuses": sorted(row.status for row in approvals),
                "approvals": [
                    {
                        "id": str(row.id),
                        "workflow_id": str(row.workflow_run_id),
                        "reviewer_id": str(row.reviewer_id),
                        "status": row.status,
                        "original_sha256": _digest(row.original_note),
                        "approved_sha256": _digest(row.approved_note),
                        "rejection_sha256": _digest(row.rejection_reason),
                        "timestamp": row.timestamp.isoformat(),
                    }
                    for row in approvals
                ],
                "missing_approval_present": (
                    await session.get(ApprovalModel, fixture.missing_approval_id) is not None
                ),
                "final_notes": [
                    {
                        "id": str(row.id),
                        "approval_id": str(row.approval_id),
                        "sha256": _digest(row.note),
                    }
                    for row in finals
                ],
            }
