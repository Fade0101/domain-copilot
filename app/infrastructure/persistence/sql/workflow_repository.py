"""PostgreSQL repository for WorkflowRun aggregates (Ticket #17)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.clinical_tools.contracts import FinalizeClinicalNoteOutput
from app.application.ports.workflow import IWorkflowRunRepository
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState
from app.infrastructure.persistence.models import FinalClinicalNoteModel, WorkflowRunModel


def _to_entity(row: WorkflowRunModel) -> WorkflowRun:
    return WorkflowRun(
        id=row.id,
        user_id=row.user_id,
        correlation_id=row.correlation_id,
        case_summary=row.case_summary,
        created_at=row.created_at,
        updated_at=row.created_at,
        # #19 persists APPROVED as its handoff marker. #17 represents approval
        # as a decision, retaining AWAITING_APPROVAL until its guarded resume.
        state=ClinicalWorkflowState.AWAITING_APPROVAL
        if row.state == "APPROVED"
        else ClinicalWorkflowState(row.state),
        approval_job_id=row.approval_job_id,
        review_snapshot=row.review_snapshot,
    )


class PostgresWorkflowRunRepository(IWorkflowRunRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_final_note(self, workflow_id: UUID) -> FinalizeClinicalNoteOutput | None:
        async with self._session_factory() as session:
            note = await session.scalar(
                sa.select(FinalClinicalNoteModel).where(
                    FinalClinicalNoteModel.workflow_run_id == workflow_id
                )
            )
            if note is None:
                return None
            return FinalizeClinicalNoteOutput(
                note.id,
                note.workflow_run_id,
                note.draft_id,
                note.approval_id,
                note.note,
                note.finalized_by,
                note.finalized_at,
                False,
            )

    async def get_by_id(self, workflow_id: UUID) -> WorkflowRun | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                sa.select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
            )
            return _to_entity(row) if row is not None else None

    async def get_by_job_id(self, job_id: UUID) -> WorkflowRun | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                sa.select(WorkflowRunModel).where(WorkflowRunModel.approval_job_id == job_id)
            )
            return _to_entity(row) if row is not None else None

    async def save(self, run: WorkflowRun) -> None:
        async with self._session_factory() as session, session.begin():
            stmt = (
                pg_insert(WorkflowRunModel)
                .values(
                    id=run.id,
                    user_id=run.user_id,
                    correlation_id=run.correlation_id,
                    case_summary=run.case_summary,
                    state=run.state.value,
                    approval_job_id=run.approval_job_id,
                    # SQL NULL means no review yet. JSON null would trip #19's
                    # immutable-snapshot trigger on the first real review write.
                    review_snapshot=run.review_snapshot
                    if run.review_snapshot is not None
                    else sa.null(),
                    created_at=run.created_at,
                )
                .on_conflict_do_update(
                    index_elements=[WorkflowRunModel.id],
                    set_={
                        "state": run.state.value,
                        "approval_job_id": run.approval_job_id,
                        "review_snapshot": run.review_snapshot
                        if run.review_snapshot is not None
                        else sa.null(),
                    },
                )
            )
            await session.execute(stmt)

    async def update_state(
        self,
        workflow_id: UUID,
        state: ClinicalWorkflowState,
        now: datetime,
        *,
        approval_decision: str | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(WorkflowRunModel)
                .where(WorkflowRunModel.id == workflow_id)
                .values(state=state.value)
            )

    async def bind_job(self, workflow_id: UUID, job_id: UUID, now: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(WorkflowRunModel)
                .where(WorkflowRunModel.id == workflow_id)
                .values(approval_job_id=job_id)
            )
