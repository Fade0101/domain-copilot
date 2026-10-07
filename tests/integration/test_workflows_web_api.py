"""Ticket #25's minimal HTTP handoff uses real SQL, JWT and approval/finalization."""

import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.domain.auth.value_objects import Role
from app.domain.jobs.entities import Job, JobState
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState
from app.infrastructure.persistence.job_store import job_insert_values
from app.infrastructure.persistence.models import (
    FinalClinicalNoteModel,
    JobModel,
    UserModel,
    WorkflowRunModel,
)
from tests.integration.test_approvals_api import Gate
from tests.integration.test_approvals_api import approval_database_url as approval_database_url
from tests.integration.test_approvals_api import gate as gate
from tests.support.approval_fixtures import review_snapshot


async def other_analyst(gate: Gate) -> dict[str, str]:
    identifier = uuid4()
    async with gate.sessions() as session, session.begin():
        session.add(
            UserModel(
                id=identifier,
                email=f"{identifier}@example.com",
                hashed_password="unused-test-digest",
                role="analyst",
            )
        )
    token = gate.container.token_service.issue(subject=str(identifier), role="analyst")
    return {"Authorization": f"Bearer {token.access_token}"}


async def test_new_workflow_uses_sql_null_until_first_immutable_review(gate: Gate) -> None:
    snapshot = review_snapshot()
    identifier = snapshot.draft.workflow_id
    principal = gate.principals[Role.ANALYST]
    owner = UUID(principal.user_id.value)
    now = gate.container._clock.now()
    job = Job(uuid4(), "clinical.workflow", {}, owner, now, now, state=JobState.STARTED)
    async with gate.sessions() as session, session.begin():
        session.add(JobModel(**job_insert_values(job)))
    await gate.container.workflow_repository.save(
        WorkflowRun(
            identifier,
            owner,
            str(uuid4()),
            "Synthetic case",
            now,
            now,
            state=ClinicalWorkflowState.AWAITING_APPROVAL,
            approval_job_id=job.id,
        )
    )
    async with gate.sessions() as session:
        assert (
            await session.scalar(
                sa.select(WorkflowRunModel.review_snapshot.is_(None)).where(
                    WorkflowRunModel.id == identifier
                )
            )
            is True
        )
    review = await gate.service.prepare_review(snapshot, job.id, principal)
    assert review.snapshot == snapshot


async def test_workflow_status_and_note_enforce_owner_and_stored_roles(gate: Gate) -> None:
    snapshot, _ = await gate.register()
    base = f"/api/v1/runs/{snapshot.draft.workflow_id}"
    outsider = await other_analyst(gate)
    for path in ("status", "note", "resume"):
        method = gate.client.post if path == "resume" else gate.client.get
        assert (await method(f"{base}/{path}")).status_code == 401
        assert (await method(f"{base}/{path}", headers=outsider)).status_code == 403
    for role in Role:
        response = await gate.client.get(f"{base}/status", headers=gate.auth(role))
        assert response.status_code == 200
        assert response.json()["state"] == "AWAITING_APPROVAL"
        assert response.headers["cache-control"] == "no-store"
    assert (
        await gate.client.get(f"{base}/note", headers=gate.auth(Role.ANALYST))
    ).status_code == 404


async def test_approved_is_not_finalized_and_existing_handoff_maps_to_workflow_phase(
    gate: Gate,
) -> None:
    snapshot, _ = await gate.register()
    base = f"/api/v1/runs/{snapshot.draft.workflow_id}"
    assert (
        await gate.client.post(f"{base}/resume", headers=gate.auth(Role.ANALYST))
    ).status_code == 400
    decision = await gate.decide(snapshot, "edit-and-approve")
    assert decision.status_code == 200
    # #19 persists APPROVED; #17 treats it as a decision on the awaiting phase.
    status = await gate.client.get(f"{base}/status", headers=gate.auth(Role.ANALYST))
    assert status.status_code == 200
    assert status.json()["state"] == "AWAITING_APPROVAL"
    assert (
        await gate.client.get(f"{base}/note", headers=gate.auth(Role.ANALYST))
    ).status_code == 404
    assert await gate.count(FinalClinicalNoteModel, snapshot.draft.workflow_id) == 0

    committed = decision.json()["review"]["decision"]
    output = await gate.finalize(snapshot, UUID(committed["id"]))
    assert output["ok"] is True
    for role in Role:
        response = await gate.client.get(f"{base}/note", headers=gate.auth(role))
        assert response.status_code == 200
        assert response.json()["note"] == committed["approved_note"]
        assert response.json()["note_id"] == output["result"]["note_id"]
        assert response.headers["cache-control"] == "no-store"


async def test_resume_calls_existing_service_only_after_committed_approval(
    gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, job_id = await gate.register()
    service = gate.container.job_service
    enqueue = AsyncMock()
    monkeypatch.setattr(service._queue, "enqueue", enqueue)
    path = f"/api/v1/runs/{snapshot.draft.workflow_id}/resume"
    assert (await gate.client.post(path, headers=gate.auth(Role.REVIEWER))).status_code == 400
    enqueue.assert_not_awaited()
    assert (await gate.decide(snapshot)).status_code == 200
    assert (await gate.client.post(path, headers=gate.auth(Role.REVIEWER))).status_code == 202
    enqueue.assert_awaited_once_with(job_id)
    assert await gate.count(FinalClinicalNoteModel, snapshot.draft.workflow_id) == 0


@pytest.mark.parametrize("role", list(Role))
async def test_clinical_submission_uses_registered_handler_and_authenticated_owner(
    gate: Gate, role: Role
) -> None:
    broker = os.environ.get("T20_TEST_REDIS_URL")
    if not broker:
        pytest.skip("Set T20_TEST_REDIS_URL")
    gate.settings.queue.broker_url = broker
    gate.settings.queue.default_queue = "t25_" + uuid4().hex
    body = {"clinical_question": "Synthetic question", "case_summary": "Synthetic case"}
    response = await gate.client.post("/api/v1/runs", json=body, headers=gate.auth(role))
    assert response.status_code == 202, response.text
    accepted = response.json()
    stored = await gate.container.job_service.get(UUID(accepted["job_id"]))
    assert stored.user_id == UUID(gate.principals[role].user_id.value)
    assert stored.operation_type == "clinical.workflow"
    assert stored.input_payload["workflow_id"] == accepted["workflow_id"]
    assert response.headers["location"] == accepted["status_url"]
    for invalid in ({**body, "workflow_id": str(uuid4())}, {**body, "case_summary": "x" * 4001}):
        rejected = await gate.client.post("/api/v1/runs", json=invalid, headers=gate.auth(role))
        assert rejected.status_code == 422
