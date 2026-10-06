"""Atomic human review using existing workflow, approval, job and audit tables.

The transaction advisory lock uses the SAME key as #20's session execution
lock. All approval writes use the connection holding that lock. There is no
second locking service, broker publication, worker or finalizer in this adapter.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass as validated_dataclass
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.approvals.contracts import (
    AWAITING_APPROVAL,
    DECISION_RECORDED,
    FINALIZATION_REQUESTED,
    REVIEW_REQUESTED,
    ApprovalCommand,
    DecisionResult,
    DraftReview,
    FinalizationRequest,
    ReviewRecord,
)
from app.application.approvals.errors import ApprovalStoreError
from app.application.approvals.rules import (
    decision_permissions,
    is_same_decision,
    make_decision,
    textual_diff,
    validate_command,
)
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import draft_digest
from app.application.errors import ResourceNotFoundError, UnknownPrincipalError
from app.application.ports.approvals import IApprovalStore
from app.application.ports.audit import AuditEntry
from app.application.ports.system import IClock
from app.domain.approvals.entities import ApprovalAction, ApprovalDecision, ApprovalStatus
from app.domain.auth.value_objects import Permission, ResourceType, Role, UserId
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError
from app.infrastructure.persistence.job_store import job_execution_lock_key, job_from_mapping
from app.infrastructure.persistence.models import (
    ApprovalModel,
    FinalClinicalNoteModel,
    JobEventModel,
    JobModel,
    UserModel,
    WorkflowRunModel,
)
from app.infrastructure.persistence.sql.clinical_note_writer import _principal

MAX_REVIEW_BYTES = 4_194_304


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported review value")


def _json_object(value: object) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads(json.dumps(value, default=_json_default, allow_nan=False))
    )


class ReviewSnapshotCodec:
    """Strict JSON roundtrip of the existing dataclasses, with no text truncation."""

    def __init__(self) -> None:
        self._adapter: TypeAdapter[DraftReview] = TypeAdapter(
            validated_dataclass(
                DraftReview,
                config=ConfigDict(extra="forbid", strict=True, revalidate_instances="always"),
                frozen=True,
            )
        )

    def encode(self, snapshot: DraftReview) -> dict[str, Any]:
        try:
            encoded = json.dumps(asdict(snapshot), default=_json_default, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_REVIEW_BYTES:
                raise ValueError("Review too large")
            self._adapter.validate_json(encoded)
            return cast(dict[str, Any], json.loads(encoded))
        except (ValidationError, ValueError, TypeError, RecursionError) as exc:
            raise InvariantViolationError("Invalid or oversized review snapshot") from exc

    def decode(self, payload: dict[str, Any]) -> DraftReview:
        try:
            validated = self._adapter.validate_json(json.dumps(payload, allow_nan=False))
            return DraftReview(validated.draft, validated.safety_verdict, validated.schema_version)
        except (ValidationError, ValueError, TypeError, InvariantViolationError):
            raise ApprovalStoreError("Persisted review snapshot is invalid") from None


class PostgresApprovalStore(IApprovalStore):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorization: AuthorizationService,
        clock: IClock,
    ) -> None:
        self._sessions = sessions
        self._authorization = authorization
        self._clock = clock
        self._codec = ReviewSnapshotCodec()

    async def _actor(
        self,
        session: AsyncSession,
        actor_id: UserId,
        workflow_id: UUID,
        permissions: tuple[Permission, ...],
    ) -> Principal:
        row = await session.scalar(
            sa.select(UserModel)
            .where(UserModel.id == UUID(actor_id.value))
            .with_for_update(read=True)
        )
        if row is None:
            raise UnknownPrincipalError("Review actor is no longer present")
        actor = _principal(row)  # Same stored-user projection used by #18's finalizer.
        for permission in permissions:
            self._authorization.require_permission(actor, permission)
        await self._authorization.require_resource_access(actor, ResourceType.RUN, str(workflow_id))
        return actor

    async def _workflow(
        self, session: AsyncSession, workflow_id: UUID, *, lock: bool = False
    ) -> WorkflowRunModel:
        query = sa.select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise ResourceNotFoundError("Workflow was not found")
        return row

    async def _job(self, session: AsyncSession, job_id: UUID, *, lock: bool = False) -> Job:
        query = sa.select(JobModel.__table__).where(JobModel.id == job_id)
        row = (
            (await session.execute(query.with_for_update() if lock else query))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFoundError("Review job was not found")
        return job_from_mapping(row)

    @staticmethod
    def _require_wait(workflow: WorkflowRunModel, job: Job) -> None:
        if workflow.user_id != job.user_id:
            raise InvalidStateTransitionError("Review workflow and job owners do not match")
        if workflow.state != AWAITING_APPROVAL or job.state != JobState.STARTED:
            raise InvalidStateTransitionError("Workflow and job are not awaiting a review decision")
        if job.cancellation_requested:
            raise InvalidStateTransitionError("A cancelled review cannot receive a decision")

    async def _append_event(
        self,
        session: AsyncSession,
        job_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
        *,
        identifier: UUID | None = None,
    ) -> JobEventModel:
        # Every writer here holds the job row lock. Reuse #6's monotonic event
        # sequence and uniqueness; #21's event transport is not implemented here.
        last = await session.scalar(
            sa.select(sa.func.max(JobEventModel.sequence_number)).where(
                JobEventModel.job_id == job_id
            )
        )
        event = JobEventModel(
            id=identifier or uuid4(),
            job_id=job_id,
            sequence_number=(last or 0) + 1,
            event_type=event_type,
            payload=payload,
            created_at=now,
        )
        session.add(event)
        await session.flush()
        return event

    async def prepare_review(
        self, snapshot: DraftReview, job_id: UUID, actor_id: UserId
    ) -> ReviewRecord:
        encoded = self._codec.encode(snapshot)
        workflow_id = snapshot.draft.workflow_id
        try:
            async with self._sessions() as session, session.begin():
                workflow = await self._workflow(session, workflow_id, lock=True)
                job = await self._job(session, job_id, lock=True)
                actor = await self._actor(
                    session, actor_id, workflow_id, (Permission.RUN_WORKFLOW,)
                )
                if workflow.review_snapshot is not None:
                    if workflow.review_snapshot != encoded or workflow.approval_job_id != job_id:
                        raise InvalidStateTransitionError("The persisted review cannot be replaced")
                    return await self._record(session, workflow)
                self._require_wait(workflow, job)
                existing = await session.scalar(
                    sa.select(ApprovalModel.id)
                    .where(ApprovalModel.workflow_run_id == workflow_id)
                    .limit(1)
                )
                finalized = await session.scalar(
                    sa.select(FinalClinicalNoteModel.id).where(
                        FinalClinicalNoteModel.workflow_run_id == workflow_id
                    )
                )
                if existing is not None or finalized is not None:
                    raise InvalidStateTransitionError(
                        "A reviewed workflow cannot be registered again"
                    )
                workflow.approval_job_id = job_id
                workflow.review_snapshot = encoded
                now = self._clock.now()
                entry = AuditEntry(
                    actor_id=actor.user_id.value,
                    actor_role=actor.role.value,
                    action=REVIEW_REQUESTED,
                    outcome="PENDING",
                    occurred_at=now,
                    resource_type="run",
                    resource_id=str(workflow_id),
                    correlation_id=str(job_id),
                    detail={"draft_id": snapshot.draft.draft_id},
                )
                await self._append_event(
                    session, job_id, REVIEW_REQUESTED, _json_object(asdict(entry)), now
                )
                record = await self._record(session, workflow)
            return record
        except IntegrityError:
            raise InvalidStateTransitionError(
                "The workflow or job already has a registered review"
            ) from None
        except SQLAlchemyError:
            raise ApprovalStoreError("Review storage is unavailable") from None

    async def get(self, workflow_id: UUID, actor_id: UserId) -> ReviewRecord:
        try:
            async with self._sessions() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                workflow = await self._workflow(session, workflow_id)
                await self._actor(session, actor_id, workflow_id, (Permission.VIEW_OWN_RUNS,))
                result = await self._record(session, workflow)
            return result
        except SQLAlchemyError:
            raise ApprovalStoreError("Review storage is unavailable") from None

    async def _record(self, session: AsyncSession, workflow: WorkflowRunModel) -> ReviewRecord:
        if workflow.review_snapshot is None or workflow.approval_job_id is None:
            raise InvalidStateTransitionError(
                "No persisted reviewable draft exists for this workflow"
            )
        snapshot = self._codec.decode(workflow.review_snapshot)
        if snapshot.draft.workflow_id != workflow.id:
            raise ApprovalStoreError("Persisted review identity is invalid")
        job = await self._job(session, workflow.approval_job_id)
        if job.user_id != workflow.user_id:
            raise ApprovalStoreError("Persisted review job binding is invalid")
        row = await session.scalar(
            sa.select(ApprovalModel).where(
                ApprovalModel.workflow_run_id == workflow.id, ApprovalModel.action.is_not(None)
            )
        )
        decision = self._decision(row, snapshot) if row is not None else None
        signal = None
        if decision is not None and decision.status == ApprovalStatus.APPROVED:
            event = await session.scalar(
                sa.select(JobEventModel).where(
                    JobEventModel.job_id == job.id,
                    JobEventModel.event_type == FINALIZATION_REQUESTED,
                )
            )
            if event is None:
                raise ApprovalStoreError("Persisted approval signal is missing")
            signal = FinalizationRequest(
                event.id, workflow.id, job.id, decision.id, decision.draft_id, event.created_at
            )
            if event.payload != _json_object(asdict(signal)):
                raise ApprovalStoreError("Persisted approval signal identity is invalid")
        return ReviewRecord(
            workflow.id,
            job.id,
            workflow.state,
            job.state,
            snapshot,
            decision,
            signal,
            job.cancellation_requested,
        )

    @staticmethod
    def _decision(row: ApprovalModel, snapshot: DraftReview) -> ApprovalDecision:
        try:
            if row.action is None or row.reviewer_role is None:
                raise ValueError("Incomplete decision identity")
            action, status = ApprovalAction(row.action), ApprovalStatus(row.status)
            if (
                row.draft_id != snapshot.draft.draft_id
                or row.original_note != snapshot.draft.note
                or status
                != (
                    ApprovalStatus.REJECTED
                    if action == ApprovalAction.REJECT
                    else ApprovalStatus.APPROVED
                )
                or row.approved_draft_id
                != (draft_digest(row.approved_note) if row.approved_note is not None else None)
                or row.diff
                != (
                    textual_diff(row.original_note, row.approved_note)
                    if row.approved_note is not None
                    else None
                )
            ):
                raise ValueError("Decision does not match its review")
            return ApprovalDecision(
                row.id,
                row.workflow_run_id,
                row.draft_id,
                row.reviewer_id,
                Role(row.reviewer_role),
                action,
                status,
                row.original_note,
                row.approved_note,
                row.approved_draft_id,
                row.rejection_reason,
                row.diff,
                row.timestamp,
            )
        except (ValueError, TypeError):
            raise ApprovalStoreError("Persisted approval decision is invalid") from None

    async def decide(self, command: ApprovalCommand, actor_id: UserId) -> DecisionResult:
        try:
            async with self._sessions() as session, session.begin():
                # Read only the binding first. Acquire the job lock BEFORE row
                # locks: the worker may still be committing its approval pause.
                job_id = await session.scalar(
                    sa.select(WorkflowRunModel.approval_job_id).where(
                        WorkflowRunModel.id == command.workflow_id
                    )
                )
                if job_id is None:
                    raise InvalidStateTransitionError(
                        "No persisted reviewable draft exists for this workflow"
                    )
                acquired = await session.scalar(
                    sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": job_execution_lock_key(job_id)},
                )
                if not acquired:
                    raise InvalidStateTransitionError(
                        "Review job is executing; retry the decision later"
                    )
                workflow = await self._workflow(session, command.workflow_id, lock=True)
                if workflow.approval_job_id != job_id:
                    raise InvalidStateTransitionError("Review job binding changed")
                # Match #18's lock order: workflow, approval, then actor records.
                await session.execute(
                    sa.select(ApprovalModel.id)
                    .where(ApprovalModel.workflow_run_id == workflow.id)
                    .with_for_update()
                )
                job = await self._job(session, job_id, lock=True)
                actor = await self._actor(
                    session, actor_id, workflow.id, decision_permissions(command.action)
                )
                validate_command(command)
                record = await self._record(session, workflow)
                if record.decision is not None:
                    if not is_same_decision(record.decision, command, actor):
                        raise InvalidStateTransitionError(
                            "A terminal approval decision cannot be overwritten"
                        )
                    return DecisionResult(record, replayed=True)
                self._require_wait(workflow, job)
                if await session.scalar(
                    sa.select(FinalClinicalNoteModel.id).where(
                        FinalClinicalNoteModel.workflow_run_id == workflow.id
                    )
                ):
                    raise InvalidStateTransitionError(
                        "A finalized workflow cannot receive another decision"
                    )
                decision = make_decision(
                    command, record.snapshot, actor, uuid4(), self._clock.now()
                )
                session.add(
                    ApprovalModel(
                        id=decision.id,
                        workflow_run_id=workflow.id,
                        reviewer_id=decision.actor_id,
                        original_note=decision.original_note,
                        approved_note=decision.approved_note,
                        status=decision.status.value,
                        rejection_reason=decision.reason,
                        action=decision.action.value,
                        reviewer_role=decision.actor_role.value,
                        draft_id=decision.draft_id,
                        approved_draft_id=decision.approved_draft_id,
                        diff=decision.diff,
                        timestamp=decision.created_at,
                    )
                )
                entry = AuditEntry(
                    actor_id=str(decision.actor_id),
                    actor_role=decision.actor_role.value,
                    action="approval." + decision.action.value.lower(),
                    outcome=decision.status.value,
                    occurred_at=decision.created_at,
                    resource_type="run",
                    resource_id=str(workflow.id),
                    correlation_id=str(job.id),
                    detail={
                        "approval_id": str(decision.id),
                        "draft_id": decision.draft_id,
                        "approved_draft_id": decision.approved_draft_id or "",
                        "reason": decision.reason or "",
                        "diff": decision.diff or "",
                    },
                )
                await self._append_event(
                    session,
                    job.id,
                    DECISION_RECORDED,
                    _json_object(asdict(entry)),
                    decision.created_at,
                )
                workflow.state = decision.status.value
                if decision.status == ApprovalStatus.REJECTED:
                    completed = job.transition(
                        JobState.COMPLETED,
                        decision.created_at,
                        result={
                            "workflow_id": str(workflow.id),
                            "workflow_state": "REJECTED",
                            "approval_id": str(decision.id),
                        },
                    )
                    await session.execute(
                        sa.update(JobModel)
                        .where(JobModel.id == job.id)
                        .values(
                            state=completed.state.value,
                            result_payload=completed.result_payload,
                            updated_at=completed.updated_at,
                            completed_at=completed.completed_at,
                            last_error=completed.last_error,
                        )
                    )
                else:
                    signal = FinalizationRequest(
                        uuid4(),
                        workflow.id,
                        job.id,
                        decision.id,
                        decision.draft_id,
                        decision.created_at,
                    )
                    await self._append_event(
                        session,
                        job.id,
                        FINALIZATION_REQUESTED,
                        _json_object(asdict(signal)),
                        decision.created_at,
                        identifier=signal.event_id,
                    )
                await session.flush()
                result = DecisionResult(await self._record(session, workflow), replayed=False)
            # The caller receives a signal only once its decision and audit commit.
            return result
        except SQLAlchemyError:
            raise ApprovalStoreError("Review storage is unavailable") from None
