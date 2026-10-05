"""Atomic PostgreSQL approval verification and the sole clinical final-note writer."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    FinalizeClinicalNoteInput,
    FinalizeClinicalNoteOutput,
)
from app.application.clinical_tools.errors import ClinicalToolStoreError, FinalNoteConflictError
from app.application.clinical_tools.finalization import ApprovalSnapshot, verified_approved_note
from app.application.errors import ResourceNotFoundError, UnknownPrincipalError
from app.application.ports.clinical_tools import IFinalClinicalNoteWriter
from app.application.ports.system import IClock
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, Permission, ResourceType, Role, UserId
from app.domain.shared.errors import ApprovalRequiredError
from app.infrastructure.persistence.models import (
    ApprovalModel,
    FinalClinicalNoteModel,
    UserModel,
    WorkflowRunModel,
)


def _principal(row: UserModel) -> Principal:
    return Principal.from_user(
        User(
            id=UserId(str(row.id)),
            email=EmailAddress(row.email),
            role=Role(row.role),
            hashed_password=row.hashed_password,
            created_at=row.created_at,
        )
    )


def _output(note: FinalClinicalNoteModel, *, created: bool) -> FinalizeClinicalNoteOutput:
    return FinalizeClinicalNoteOutput(
        note.id,
        note.workflow_run_id,
        note.draft_id,
        note.approval_id,
        note.note,
        note.finalized_by,
        note.finalized_at,
        created,
    )


class PostgresFinalClinicalNoteWriter(IFinalClinicalNoteWriter):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorization: AuthorizationService,
        clock: IClock,
    ) -> None:
        self._sessions = sessions
        self._authorization = authorization
        self._clock = clock

    async def finalize(
        self, request: FinalizeClinicalNoteInput, actor_id: UserId
    ) -> FinalizeClinicalNoteOutput:
        try:
            async with self._sessions() as session, session.begin():
                # Serialize finalization for this workflow and keep ownership
                # stable until commit. Approval is separately locked, so a
                # concurrent decision/edit must complete BEFORE our verification
                # or wait until AFTER this transaction has written its snapshot.
                workflow = await session.scalar(
                    select(WorkflowRunModel)
                    .where(WorkflowRunModel.id == request.workflow_id)
                    .with_for_update()
                )
                if workflow is None:
                    raise ResourceNotFoundError("Workflow was not found")
                approval = await session.scalar(
                    select(ApprovalModel)
                    .where(ApprovalModel.id == request.approval_id)
                    .with_for_update()
                )
                snapshot = (
                    None
                    if approval is None
                    else ApprovalSnapshot(
                        approval.id,
                        approval.workflow_run_id,
                        approval.status,
                        approval.original_note,
                        approval.approved_note,
                    )
                )
                actor_uuid = UUID(actor_id.value)
                person_ids = {actor_uuid}
                if approval is not None:
                    person_ids.add(approval.reviewer_id)
                people = (
                    await session.scalars(
                        select(UserModel)
                        .where(UserModel.id.in_(person_ids))
                        .order_by(UserModel.id)
                        .with_for_update(read=True)
                    )
                ).all()
                by_id = {person.id: person for person in people}
                if actor_uuid not in by_id:
                    raise UnknownPrincipalError("Tool actor is no longer present")
                principal = _principal(by_id[actor_uuid])
                # Reuse #5 policy; no role comparisons or locally invented grants.
                # The locked workflow row cannot change under this ownership read.
                self._authorization.require_permission(principal, Permission.RUN_WORKFLOW)
                await self._authorization.require_resource_access(
                    principal, ResourceType.RUN, str(workflow.id)
                )
                # Always verify, including a replay when a final row already exists.
                reviewed_note = verified_approved_note(request, snapshot)
                assert approval is not None  # established by the guard above
                if approval.reviewer_id not in by_id:
                    raise ApprovalRequiredError("The approval has no attributable reviewer")
                reviewer = _principal(by_id[approval.reviewer_id])
                self._authorization.require_permission(reviewer, Permission.APPROVE_CLINICAL_NOTE)
                existing = await session.scalar(
                    select(FinalClinicalNoteModel).where(
                        FinalClinicalNoteModel.workflow_run_id == workflow.id
                    )
                )
                if existing is not None:
                    if (
                        existing.approval_id != approval.id
                        or existing.draft_id != request.draft_id
                        or existing.note != reviewed_note
                    ):
                        raise FinalNoteConflictError("A final clinical note cannot be overwritten")
                    return _output(existing, created=False)
                note = FinalClinicalNoteModel(
                    id=uuid4(),
                    workflow_run_id=workflow.id,
                    approval_id=approval.id,
                    draft_id=request.draft_id,
                    note=reviewed_note,
                    finalized_by=actor_uuid,
                    finalized_at=self._clock.now(),
                )
                session.add(note)
                await session.flush()
                return _output(note, created=True)
        except SQLAlchemyError as exc:
            raise ClinicalToolStoreError("Clinical note storage is unavailable") from exc
