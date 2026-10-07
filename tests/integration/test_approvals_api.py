"""Real SQL/JWT approval gate; no mocked approval, authorization or finalization.

The fixtures register typed agent outputs on existing awaiting runs because #17
does not exist yet. They do not pretend to execute the clinical pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from app.application.approvals.contracts import (
    DECISION_RECORDED,
    FINALIZATION_REQUESTED,
    ApprovalCommand,
    DraftReview,
)
from app.application.approvals.errors import ApprovalStoreError
from app.application.approvals.rules import textual_diff
from app.application.approvals.service import ApprovalService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import ToolName, draft_digest
from app.application.errors import PermissionDeniedError, ResourceOwnershipError
from app.application.ports.llm import ToolCall
from app.core.config import AuthSettings, DatabaseSettings, LLMSettings, Settings
from app.core.container import Container
from app.domain.approvals.entities import ApprovalAction
from app.domain.auth.value_objects import Role, UserId
from app.domain.jobs.entities import Job, JobState
from app.domain.shared.errors import InvalidStateTransitionError
from app.infrastructure.persistence.job_store import (
    PostgresJobStore,
    create_job_engine,
    job_insert_values,
)
from app.infrastructure.persistence.models import (
    ApprovalModel,
    FinalClinicalNoteModel,
    JobEventModel,
    JobModel,
    UserModel,
    WorkflowRunModel,
)
from app.infrastructure.persistence.sql.approval_store import PostgresApprovalStore
from app.presentation.api import dependencies, security
from app.presentation.api.app import create_app
from tests.integration.test_job_queue import migrate
from tests.support.approval_fixtures import (
    EDITED_NOTE,
    ORIGINAL_NOTE,
    REJECTION_REASON,
    review_snapshot,
)
from tests.support.sse import live_stream

ACTIONS = ("approve", "reject", "edit-and-approve")


@pytest.fixture(scope="module")
def approval_database_url() -> Iterator[str]:
    source = os.environ.get("TEST_DATABASE_URL")
    if not source:
        if os.environ.get("CI"):
            pytest.fail("CI must provide PostgreSQL for the human approval gate")
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector service")
    name = "t19_approval_" + uuid4().hex
    admin = sa.engine.make_url(source).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(admin, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = admin.set(database=name)
        # Additive migration is reversible before reviews exist, and repeatable.
        migrate(url.render_as_string(hide_password=False))
        migrate(url.render_as_string(hide_password=False), "-1", downgrade=True)
        migrate(url.render_as_string(hide_password=False))
        yield url.set(drivername="postgresql").render_as_string(hide_password=False)
    finally:
        with engine.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()


@dataclass
class Gate:
    container: Container
    client: httpx.AsyncClient
    principals: dict[Role, Principal]
    tokens: dict[Role, str]
    url: str
    settings: Settings

    @property
    def sessions(self) -> async_sessionmaker:
        assert self.container.database is not None
        return self.container.database.session_factory

    @property
    def service(self) -> ApprovalService:
        return self.container.approval_service()

    def auth(self, role: Role | None) -> dict[str, str]:
        return {} if role is None else {"Authorization": "Bearer " + self.tokens[role]}

    async def register(self, kind: str = "safe") -> tuple[DraftReview, UUID]:
        snapshot, job_id = review_snapshot(kind=kind), uuid4()
        now = self.container._clock.now()
        owner = UUID(self.principals[Role.ANALYST].user_id.value)
        job = Job(
            job_id,
            "test.clinical_review",
            {},
            owner,
            now,
            now,
            state=JobState.STARTED,
            attempt_number=1,
            started_at=now,
            checkpoint_data={
                "completed_stages": ["research", "safety", "draft"],
                "iterations": 7,
                "retries": {"research": 2},
            },
        )
        async with self.sessions() as session, session.begin():
            session.add(JobModel(**job_insert_values(job)))
            session.add(
                WorkflowRunModel(
                    id=snapshot.draft.workflow_id,
                    user_id=owner,
                    correlation_id=str(uuid4()),
                    case_summary="Raw case text must never replace the persisted review.",
                    state="AWAITING_APPROVAL",
                )
            )
        await self.service.prepare_review(snapshot, job_id, self.principals[Role.ANALYST])
        return snapshot, job_id

    async def decide(
        self,
        snapshot: DraftReview,
        action: str = "approve",
        role: Role | None = Role.REVIEWER,
        *,
        extra: dict[str, Any] | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"draft_id": snapshot.draft.draft_id}
        if action == "reject":
            body["reason"] = REJECTION_REASON
        if action == "edit-and-approve":
            body["edited_note"] = EDITED_NOTE
        body.update(extra or {})
        return await self.client.post(
            f"/api/v1/runs/{snapshot.draft.workflow_id}/approval/{action}",
            json=body,
            headers=self.auth(role),
        )

    async def count(self, model: Any, workflow_id: UUID) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(model)
                    .where(model.workflow_run_id == workflow_id)
                )
                or 0
            )

    async def events(self, job_id: UUID) -> list[JobEventModel]:
        """Approval audit/outbox history; public progress is tested separately."""
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    sa.select(JobEventModel)
                    .where(
                        JobEventModel.job_id == job_id,
                        JobEventModel.event_type.like("approval.%"),
                    )
                    .order_by(JobEventModel.sequence_number)
                )
            )

    async def finalize(self, snapshot: DraftReview, approval_id: UUID) -> dict[str, Any]:
        scope = self.container.clinical_tool_factory().for_orchestrator(
            self.principals[Role.ANALYST], snapshot.draft.workflow_id
        )
        result = await scope.execute(
            ToolCall(
                "t19-finalization-test",
                ToolName.FINALIZE_CLINICAL_NOTE.value,
                json.dumps(
                    {
                        "workflow_id": str(snapshot.draft.workflow_id),
                        "draft_id": snapshot.draft.draft_id,
                        "approval_id": str(approval_id),
                    }
                ),
            )
        )
        return dict(json.loads(result.output))


@pytest.fixture
async def gate(approval_database_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Gate]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=approval_database_url),
        llm=LLMSettings(provider="ollama", fallback=None),
        auth=AuthSettings(
            secret_key=SecretStr("ticket19-test-only-signing-value"), seed_demo_accounts=False
        ),
    )
    container = Container(settings)
    assert container.database is not None
    principals, tokens = {}, {}
    for role in Role:
        identifier = uuid4()
        async with container.database.session_factory() as session, session.begin():
            session.add(
                UserModel(
                    id=identifier,
                    email=f"{identifier}@example.com",
                    hashed_password="unused-test-digest",
                    role=role.value,
                )
            )
        user = await container.user_repository.get_by_id(UserId(str(identifier)))
        assert user is not None
        principals[role] = Principal.from_user(user)
        tokens[role] = container.token_service.issue(
            subject=str(identifier), role=role.value
        ).access_token
    monkeypatch.setattr(dependencies, "get_container", lambda: container)
    monkeypatch.setattr(security, "get_container", lambda: container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as client:
        yield Gate(container, client, principals, tokens, approval_database_url, settings)
    await container.dispose()


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("role", [Role.REVIEWER, Role.ADMIN, Role.ANALYST, None])
async def test_api_authorization_matrix_and_durable_actor(
    gate: Gate, action: str, role: Role | None
) -> None:
    snapshot, job_id = await gate.register()
    response = await gate.decide(snapshot, action, role)
    if role in {Role.REVIEWER, Role.ADMIN}:
        assert response.status_code == 200, response.text
        data = response.json()["review"]
        expected = "REJECTED" if action == "reject" else "APPROVED"
        assert data["approval_status"] == data["workflow_state"] == expected
        assert data["job_state"] == ("COMPLETED" if action == "reject" else "STARTED")
        assert data["decision"]["actor_id"] == gate.principals[role].user_id.value
        assert data["decision"]["actor_role"] == role.value
        events = await gate.events(job_id)
        decision_event = next(item for item in events if item.event_type == DECISION_RECORDED)
        assert decision_event.payload["actor_id"] == data["decision"]["actor_id"]
        assert decision_event.payload["action"] == "approval." + action.replace("-", "_")
        assert decision_event.payload["outcome"] == expected
        assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 1
    else:
        assert response.status_code == (401 if role is None else 403)
        assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0
        assert len(await gate.events(job_id)) == 1
    # Approvals never silently execute the privileged finalizer.
    assert await gate.count(FinalClinicalNoteModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize("kind", ["safe", "flagged", "unsupported", "capacity"])
async def test_review_reads_complete_persisted_snapshot(gate: Gate, kind: str) -> None:
    snapshot, _ = await gate.register(kind)
    response = await gate.client.get(
        f"/api/v1/runs/{snapshot.draft.workflow_id}/approval", headers=gate.auth(Role.REVIEWER)
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["draft"]["note"] == snapshot.draft.note
    assert len(data["draft"]["citations"]) == len(snapshot.draft.citations)
    assert len(data["draft"]["excluded_claims"]) == len(snapshot.draft.excluded_claims)
    assert len(data["draft"]["deferred_claims"]) == len(snapshot.draft.deferred_claims)
    assert len(data["safety_verdict"]["flags"]) == len(snapshot.safety_verdict.flags)
    assert data["draft"]["evidence_trace_ids"] == [
        str(x) for x in snapshot.draft.evidence_trace_ids
    ]
    assert data["approval_allowed"] == (kind == "safe")
    assert data["approval_status"] == "PENDING" and data["workflow_state"] == "AWAITING_APPROVAL"
    assert response.headers["cache-control"] == "no-store"


async def test_rejection_completes_job_preserves_counters_and_audits_reason(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    response = await gate.decide(snapshot, "reject")
    assert response.status_code == 200
    async with gate.sessions() as session:
        job = await session.get(JobModel, job_id)
        workflow = await session.get(WorkflowRunModel, snapshot.draft.workflow_id)
        assert job is not None and workflow is not None
        assert workflow.state == "REJECTED" and job.state == "COMPLETED"
        assert job.completed_at is not None and job.last_error is None
        assert job.attempt_number == 1 and job.checkpoint_data["iterations"] == 7
        assert job.checkpoint_data["retries"] == {"research": 2}
        assert job.result_payload["workflow_state"] == "REJECTED"
    events = await gate.events(job_id)
    assert events[-1].payload["detail"]["reason"] == REJECTION_REASON
    assert all(event.event_type != FINALIZATION_REQUESTED for event in events)
    decision = await gate.service.get_decision(
        snapshot.draft.workflow_id, gate.principals[Role.ANALYST]
    )
    assert decision is not None and decision.reason == REJECTION_REASON
    assert (await gate.finalize(snapshot, decision.id))["error"]["code"] == "APPROVAL_REQUIRED"


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_sse_observes_approval_phase_without_resuming_or_cancelling(
    gate: Gate, action: str
) -> None:
    snapshot, job_id = await gate.register()
    async with live_stream(
        create_app(gate.settings), f"/api/v1/jobs/{job_id}/stream", gate.auth(Role.ANALYST)
    ) as stream:
        await stream.wait_for(
            lambda frames: any(
                frame.data.get("workflow_state") == "AWAITING_APPROVAL" for frame in frames
            )
        )
        awaiting = stream.frames[-1]
        assert awaiting.event == "job_progress"
        assert awaiting.data["state"] == "STARTED"
        assert awaiting.data["workflow_id"] == str(snapshot.draft.workflow_id)
        assert (await gate.decide(snapshot, action)).status_code == 200
        expected = "REJECTED" if action == "reject" else "APPROVED"
        await stream.wait_for(
            lambda frames: any(frame.data.get("workflow_state") == expected for frame in frames)
        )
        decision = stream.frames[-1]
        assert decision.sequence > awaiting.sequence
        assert decision.data["state"] == ("COMPLETED" if action == "reject" else "STARTED")
        assert all(frame.event == "job_progress" for frame in stream.frames)
        assert all("detail" not in frame.data for frame in stream.frames)
    async with gate.sessions() as session:
        job = await session.get(JobModel, job_id)
        assert job is not None and not job.cancellation_requested
        assert job.checkpoint_data["iterations"] == 7 and job.attempt_number == 1
    assert await gate.count(FinalClinicalNoteModel, snapshot.draft.workflow_id) == 0


async def test_edit_and_approve_survives_new_container_with_original_and_diff(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    response = await gate.decide(snapshot, "edit-and-approve")
    assert response.status_code == 200, response.text
    fresh = Container(gate.settings)
    try:
        record = await fresh.approval_service().get_review(
            snapshot.draft.workflow_id, gate.principals[Role.REVIEWER]
        )
        assert record.snapshot == snapshot and record.decision is not None
        decision = record.decision
        assert decision.original_note == ORIGINAL_NOTE and decision.approved_note == EDITED_NOTE
        assert decision.diff == textual_diff(ORIGINAL_NOTE, EDITED_NOTE)
        assert decision.draft_id == draft_digest(ORIGINAL_NOTE)
        assert decision.approved_draft_id == draft_digest(EDITED_NOTE)
        assert record.finalization_request is not None
        assert record.finalization_request.approval_id == decision.id
    finally:
        await fresh.dispose()
    events = await gate.events(job_id)
    assert events[1].payload["detail"]["diff"] == decision.diff
    assert events[2].event_type == FINALIZATION_REQUESTED
    result = await gate.finalize(snapshot, decision.id)
    assert result["ok"] and result["result"]["note"] == EDITED_NOTE
    replay = await gate.finalize(snapshot, decision.id)
    assert replay["ok"] and not replay["result"]["created"]


async def test_pending_review_cannot_finalize_and_client_flag_cannot_approve(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    result = await gate.finalize(snapshot, uuid4())
    assert not result["ok"] and result["error"]["code"] == "APPROVAL_REQUIRED"
    assert (await gate.decide(snapshot, extra={"approved": True})).status_code == 422
    assert (
        await gate.service.get_finalization_request(
            snapshot.draft.workflow_id, gate.principals[Role.ANALYST]
        )
        is None
    )


@pytest.mark.parametrize("action", ACTIONS)
async def test_exact_replay_returns_original_decision_without_new_events(
    gate: Gate, action: str
) -> None:
    snapshot, job_id = await gate.register()
    first = await gate.decide(snapshot, action)
    again = await gate.decide(snapshot, action)
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] and not first.json()["replayed"]
    assert first.json()["review"] == again.json()["review"]
    assert len(await gate.events(job_id)) == (2 if action == "reject" else 3)


@pytest.mark.parametrize("first,second", [(a, b) for a in ACTIONS for b in ACTIONS if a != b])
async def test_conflicting_terminal_decision_is_never_overwritten(
    gate: Gate, first: str, second: str
) -> None:
    snapshot, _ = await gate.register()
    initial = await gate.decide(snapshot, first)
    assert initial.status_code == 200
    assert (await gate.decide(snapshot, second)).status_code == 409
    record = await gate.service.get_review(
        snapshot.draft.workflow_id, gate.principals[Role.REVIEWER]
    )
    assert (
        record.decision is not None
        and str(record.decision.id) == initial.json()["review"]["decision"]["id"]
    )


async def test_different_edit_or_actor_is_not_an_idempotent_replay(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    assert (await gate.decide(snapshot, "edit-and-approve")).status_code == 200
    assert (
        await gate.decide(
            snapshot, "edit-and-approve", extra={"edited_note": "Different reviewed content."}
        )
    ).status_code == 409
    assert (await gate.decide(snapshot, "edit-and-approve", Role.ADMIN)).status_code == 409


@pytest.mark.parametrize("state", ["APPROVED", "REJECTED"])
async def test_new_decision_requires_awaiting_workflow(gate: Gate, state: str) -> None:
    snapshot, _ = await gate.register()
    async with gate.sessions() as session, session.begin():
        await session.execute(
            sa.update(WorkflowRunModel)
            .where(WorkflowRunModel.id == snapshot.draft.workflow_id)
            .values(state=state)
        )
    assert (await gate.decide(snapshot, "reject")).status_code == 409
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize(
    "state",
    [JobState.PENDING, JobState.QUEUED, JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED],
)
async def test_new_decision_requires_started_job(gate: Gate, state: JobState) -> None:
    snapshot, job_id = await gate.register()
    async with gate.sessions() as session, session.begin():
        await session.execute(
            sa.update(JobModel).where(JobModel.id == job_id).values(state=state.value)
        )
    assert (await gate.decide(snapshot, "reject")).status_code == 409


async def test_cancellation_signal_prevents_new_decision(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    async with gate.sessions() as session, session.begin():
        await session.execute(
            sa.update(JobModel).where(JobModel.id == job_id).values(cancellation_requested=True)
        )
    assert (await gate.decide(snapshot)).status_code == 409
    response = await gate.client.get(
        f"/api/v1/runs/{snapshot.draft.workflow_id}/approval", headers=gate.auth(Role.REVIEWER)
    )
    assert response.json()["cancellation_requested"]
    assert not response.json()["approval_allowed"]


@pytest.mark.parametrize("reason", [None, "", "   ", "?!?"])
async def test_reject_without_meaningful_reason_is_4xx(gate: Gate, reason: Any) -> None:
    snapshot, _ = await gate.register()
    assert (await gate.decide(snapshot, "reject", extra={"reason": reason})).status_code == 422
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize(
    "field",
    [
        "role",
        "actor_id",
        "workflow_id",
        "job_id",
        "approval_id",
        "original_note",
        "citations",
        "safety_verdict",
        "diff",
    ],
)
async def test_request_cannot_replace_identity_draft_or_provenance(gate: Gate, field: str) -> None:
    snapshot, _ = await gate.register()
    assert (await gate.decide(snapshot, extra={field: "untrusted"})).status_code == 422
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize(
    "kind", ["capacity", "refused", "flagged", "unsupported", "tool_error", "timeout"]
)
@pytest.mark.parametrize("action", ["approve", "edit-and-approve"])
async def test_refused_or_partial_draft_cannot_get_persisted_approval(
    gate: Gate, kind: str, action: str
) -> None:
    snapshot, _ = await gate.register(kind)
    assert (await gate.decide(snapshot, action)).status_code == 409
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0
    assert (await gate.decide(snapshot, "reject")).status_code == 200


async def test_current_database_role_overrules_stale_token_and_direct_principal(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    reviewer = gate.principals[Role.REVIEWER]
    async with gate.sessions() as session, session.begin():
        await session.execute(
            sa.update(UserModel)
            .where(UserModel.id == UUID(reviewer.user_id.value))
            .values(role="analyst")
        )
    assert (await gate.decide(snapshot)).status_code == 403
    with pytest.raises(PermissionDeniedError):
        await gate.service.approve(snapshot.draft.workflow_id, snapshot.draft.draft_id, reviewer)
    with pytest.raises(PermissionDeniedError):
        await gate.service.approve(
            snapshot.draft.workflow_id,
            snapshot.draft.draft_id,
            replace(gate.principals[Role.ANALYST], role=Role.ADMIN),
        )


async def test_analyst_headers_and_foreign_lookup_do_not_bypass_run_policy(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    response = await gate.client.post(
        f"/api/v1/runs/{snapshot.draft.workflow_id}/approval/approve?role=admin",
        headers={**gate.auth(Role.ANALYST), "X-Role": "admin"},
        json={"draft_id": snapshot.draft.draft_id},
    )
    assert response.status_code == 403
    identifier = uuid4()
    async with gate.sessions() as session, session.begin():
        session.add(
            UserModel(
                id=identifier,
                email=f"{identifier}@example.com",
                role="analyst",
                hashed_password="unused-test-digest",
            )
        )
    user = await gate.container.user_repository.get_by_id(UserId(str(identifier)))
    assert user is not None
    with pytest.raises(ResourceOwnershipError):
        await gate.service.get_decision(snapshot.draft.workflow_id, Principal.from_user(user))
    assert (await gate.decide(snapshot, extra={"draft_id": "0" * 64})).status_code == 409


async def test_worker_execution_lock_excludes_a_human_decision(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    engine = create_job_engine(gate.url)
    try:
        async with PostgresJobStore(engine).lock(job_id) as locked:
            assert locked is not None
            assert (await gate.decide(snapshot, "reject")).status_code == 409
        assert (await gate.decide(snapshot, "reject")).status_code == 200
    finally:
        engine.dispose()


async def test_concurrent_conflicting_decisions_commit_exactly_one(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    responses = await asyncio.gather(
        gate.decide(snapshot), gate.decide(snapshot, "reject", Role.ADMIN)
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 1
    assert sum(event.event_type == DECISION_RECORDED for event in await gate.events(job_id)) == 1


async def test_audit_failure_rolls_back_decision_workflow_and_job(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, job_id = await gate.register()
    service = gate.service
    store = service._store
    assert isinstance(store, PostgresApprovalStore)
    append = store._append_event

    async def fail_after_audit(*args: Any, **kwargs: Any) -> Any:
        await append(*args, **kwargs)
        raise SQLAlchemyError("Simulated audit storage failure")

    monkeypatch.setattr(store, "_append_event", fail_after_audit)
    with pytest.raises(ApprovalStoreError):
        await service.reject(
            snapshot.draft.workflow_id,
            snapshot.draft.draft_id,
            REJECTION_REASON,
            gate.principals[Role.REVIEWER],
        )
    record = await gate.service.get_review(
        snapshot.draft.workflow_id, gate.principals[Role.REVIEWER]
    )
    assert record.decision is None and record.workflow_state == "AWAITING_APPROVAL"
    assert record.job_state == JobState.STARTED and record.finalization_request is None
    assert len(await gate.events(job_id)) == 1


async def test_commit_failure_does_not_expose_finalization_signal(gate: Gate) -> None:
    snapshot, job_id = await gate.register()

    class FailingCommitSession(Session):
        pass

    @sa.event.listens_for(FailingCommitSession, "before_commit")
    def fail_commit(session: Session) -> None:
        raise SQLAlchemyError("Simulated commit failure")

    sessions = async_sessionmaker(
        gate.sessions.kw["bind"], expire_on_commit=False, sync_session_class=FailingCommitSession
    )
    store = PostgresApprovalStore(
        sessions, gate.container.authorization_service, gate.container._clock
    )
    service = ApprovalService(
        store,
        gate.container.user_repository,
        gate.container.authorization_service,
        gate.container._audit_sink,
        gate.container._clock,
    )
    with pytest.raises(ApprovalStoreError):
        await service.approve(
            snapshot.draft.workflow_id, snapshot.draft.draft_id, gate.principals[Role.REVIEWER]
        )
    assert (
        await gate.service.get_decision(snapshot.draft.workflow_id, gate.principals[Role.ANALYST])
        is None
    )
    assert len(await gate.events(job_id)) == 1


async def test_signal_is_invisible_until_decision_and_audit_commit(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _ = await gate.register()
    service = gate.service
    store = service._store
    assert isinstance(store, PostgresApprovalStore)
    append = store._append_event
    written, release = asyncio.Event(), asyncio.Event()

    async def pause_after_signal(*args: Any, **kwargs: Any) -> Any:
        result = await append(*args, **kwargs)
        if result.event_type == FINALIZATION_REQUESTED:
            written.set()
            await release.wait()
        return result

    monkeypatch.setattr(store, "_append_event", pause_after_signal)
    task = asyncio.create_task(
        service.approve(
            snapshot.draft.workflow_id, snapshot.draft.draft_id, gate.principals[Role.REVIEWER]
        )
    )
    try:
        async with asyncio.timeout(10):
            await written.wait()
            assert not task.done()
            assert (
                await gate.service.get_decision(
                    snapshot.draft.workflow_id, gate.principals[Role.ANALYST]
                )
                is None
            )
            assert (
                await gate.service.get_finalization_request(
                    snapshot.draft.workflow_id, gate.principals[Role.ANALYST]
                )
                is None
            )
    finally:
        release.set()
        result = await task
    assert result.review.finalization_request is not None
    assert (
        await gate.service.get_finalization_request(
            snapshot.draft.workflow_id, gate.principals[Role.ANALYST]
        )
        == result.review.finalization_request
    )


@pytest.mark.parametrize("target", ["decision", "snapshot", "audit"])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_database_protects_historical_review_decision_and_audit(
    gate: Gate, target: str, operation: str
) -> None:
    snapshot, job_id = await gate.register()
    assert (await gate.decide(snapshot)).status_code == 200
    model: type[ApprovalModel] | type[WorkflowRunModel] | type[JobEventModel]
    changes: dict[str, Any]
    if target == "decision":
        model, condition, changes = (
            ApprovalModel,
            ApprovalModel.workflow_run_id == snapshot.draft.workflow_id,
            {"approved_note": "tampered"},
        )
    elif target == "snapshot":
        model, condition, changes = (
            WorkflowRunModel,
            WorkflowRunModel.id == snapshot.draft.workflow_id,
            {"review_snapshot": {"tampered": True}},
        )
    else:
        model, condition, changes = (
            JobEventModel,
            JobEventModel.job_id == job_id,
            {"payload": {"tampered": True}},
        )
    with pytest.raises(SQLAlchemyError):
        async with gate.sessions() as session, session.begin():
            statement = (
                sa.delete(model).where(condition)
                if operation == "delete"
                else sa.update(model).where(condition).values(**changes)
            )
            await session.execute(statement)
    assert (
        await gate.service.get_review(snapshot.draft.workflow_id, gate.principals[Role.REVIEWER])
    ).snapshot == snapshot


async def test_registration_is_immutable_and_exact_replay_is_idempotent(gate: Gate) -> None:
    snapshot, job_id = await gate.register()
    await gate.service.prepare_review(snapshot, job_id, gate.principals[Role.ANALYST])
    assert len(await gate.events(job_id)) == 1
    # Same note digest, changed safety/citation data: still cannot replace history.
    replacement = review_snapshot(snapshot.draft.workflow_id)
    with pytest.raises(InvalidStateTransitionError):
        await gate.service.prepare_review(replacement, job_id, gate.principals[Role.ANALYST])


async def test_wrong_job_owner_cannot_be_registered(gate: Gate) -> None:
    snapshot = review_snapshot()
    now, job_id = gate.container._clock.now(), uuid4()
    job = Job(
        job_id,
        "test.clinical_review",
        {},
        UUID(gate.principals[Role.ADMIN].user_id.value),
        now,
        now,
        state=JobState.STARTED,
    )
    async with gate.sessions() as session, session.begin():
        session.add(JobModel(**job_insert_values(job)))
        session.add(
            WorkflowRunModel(
                id=snapshot.draft.workflow_id,
                user_id=UUID(gate.principals[Role.ANALYST].user_id.value),
                correlation_id=str(uuid4()),
                case_summary="Synthetic",
                state="AWAITING_APPROVAL",
            )
        )
    with pytest.raises(InvalidStateTransitionError):
        await gate.service.prepare_review(snapshot, job_id, gate.principals[Role.ANALYST])


async def test_job_cannot_be_bound_to_two_review_workflows(gate: Gate) -> None:
    original, job_id = await gate.register()
    other = review_snapshot()
    async with gate.sessions() as session, session.begin():
        session.add(
            WorkflowRunModel(
                id=other.draft.workflow_id,
                user_id=UUID(gate.principals[Role.ANALYST].user_id.value),
                correlation_id=str(uuid4()),
                case_summary="Synthetic",
                state="AWAITING_APPROVAL",
            )
        )
    with pytest.raises(InvalidStateTransitionError):
        await gate.service.prepare_review(other, job_id, gate.principals[Role.ANALYST])
    persisted = await gate.service.get_review(
        original.draft.workflow_id, gate.principals[Role.REVIEWER]
    )
    assert persisted.snapshot == original and persisted.job_id == job_id


async def test_downgrade_refuses_to_erase_clinical_audit(gate: Gate) -> None:
    await gate.register()
    with pytest.raises(RuntimeError, match="Cannot discard"):
        # Exercise the protected recovery/approval boundary, regardless of later heads.
        await asyncio.to_thread(migrate, gate.url, "f19b6a2d9041", downgrade=True)


async def test_approve_preserves_exact_note_and_finalizer_rechecks_reviewer_role(
    gate: Gate,
) -> None:
    snapshot, _ = await gate.register()
    response = await gate.decide(snapshot)
    assert response.status_code == 200
    decision = response.json()["review"]["decision"]
    assert decision["original_note"] == decision["approved_note"] == ORIGINAL_NOTE
    assert decision["diff"] == "" and decision["draft_id"] == decision["approved_draft_id"]
    async with gate.sessions() as session, session.begin():
        await session.execute(
            sa.update(UserModel)
            .where(UserModel.id == UUID(gate.principals[Role.REVIEWER].user_id.value))
            .values(role="analyst")
        )
    result = await gate.finalize(snapshot, UUID(decision["id"]))
    assert not result["ok"] and result["error"]["code"] == "PERMISSION_DENIED"
    assert await gate.count(FinalClinicalNoteModel, snapshot.draft.workflow_id) == 0


async def test_role_is_rechecked_in_transaction_after_service_authorization(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _ = await gate.register()
    service = gate.service
    store = service._store
    decide = store.decide

    async def demote_then_decide(command: ApprovalCommand, actor_id: UserId) -> Any:
        async with gate.sessions() as session, session.begin():
            await session.execute(
                sa.update(UserModel)
                .where(UserModel.id == UUID(actor_id.value))
                .values(role="analyst")
            )
        return await decide(command, actor_id)

    monkeypatch.setattr(store, "decide", demote_then_decide)
    with pytest.raises(PermissionDeniedError):
        await service.approve(
            snapshot.draft.workflow_id, snapshot.draft.draft_id, gate.principals[Role.REVIEWER]
        )
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0


async def test_persistence_contract_itself_enforces_approval_permission(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    command = ApprovalCommand(
        snapshot.draft.workflow_id, snapshot.draft.draft_id, ApprovalAction.APPROVE
    )
    with pytest.raises(PermissionDeniedError):
        await gate.service._store.decide(command, gate.principals[Role.ANALYST].user_id)
    assert await gate.count(ApprovalModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize("role", [Role.ANALYST, None])
async def test_review_screen_requires_reviewer_permission(gate: Gate, role: Role | None) -> None:
    snapshot, _ = await gate.register()
    response = await gate.client.get(
        f"/api/v1/runs/{snapshot.draft.workflow_id}/approval", headers=gate.auth(role)
    )
    assert response.status_code == (401 if role is None else 403)


async def test_raw_case_is_not_a_substitute_for_missing_persisted_draft(gate: Gate) -> None:
    workflow_id = uuid4()
    async with gate.sessions() as session, session.begin():
        session.add(
            WorkflowRunModel(
                id=workflow_id,
                user_id=UUID(gate.principals[Role.ANALYST].user_id.value),
                correlation_id=str(uuid4()),
                case_summary=ORIGINAL_NOTE,
                state="AWAITING_APPROVAL",
            )
        )
    response = await gate.client.get(
        f"/api/v1/runs/{workflow_id}/approval", headers=gate.auth(Role.REVIEWER)
    )
    assert response.status_code == 409
    decision = await gate.client.post(
        f"/api/v1/runs/{workflow_id}/approval/approve",
        headers=gate.auth(Role.REVIEWER),
        json={"draft_id": draft_digest(ORIGINAL_NOTE)},
    )
    assert decision.status_code == 409
    assert await gate.count(ApprovalModel, workflow_id) == 0


async def test_approval_storage_failure_has_static_503_response(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _ = await gate.register()
    service = gate.service

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ApprovalStoreError("Driver message containing private clinical content")

    monkeypatch.setattr(service._store, "decide", unavailable)
    monkeypatch.setattr(gate.container, "approval_service", lambda: service)
    response = await gate.decide(snapshot)
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Approval storage is unavailable",
        "code": "APPROVAL_STORE_UNAVAILABLE",
    }
