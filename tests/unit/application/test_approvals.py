"""Approval policy and service boundaries; database authority is integration-tested."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.approvals.contracts import (
    ApprovalCommand,
    DecisionResult,
    DraftReview,
    ReviewRecord,
)
from app.application.approvals.errors import ApprovalStoreError
from app.application.approvals.rules import (
    draft_is_approvable,
    is_same_decision,
    make_decision,
    textual_diff,
    validate_command,
)
from app.application.approvals.service import ApprovalService
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import (
    PermissionDeniedError,
    ResourceOwnershipError,
    UnknownPrincipalError,
)
from app.domain.approvals.entities import ApprovalAction, ApprovalStatus
from app.domain.auth.value_objects import ResourceType, Role, UserId
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError
from tests.support.approval_fixtures import EDITED_NOTE, REJECTION_REASON, review_snapshot
from tests.support.fakes import (
    FakeOwnershipQuery,
    FakeUserRepository,
    FixedClock,
    RecordingAuditSink,
    build_user,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)


class ApprovalStoreSpy:
    def __init__(self, snapshot: DraftReview, actor: Principal) -> None:
        self.snapshot = snapshot
        self.actor = actor
        self.calls: list[str] = []
        self.failure: Exception | None = None
        self.record = ReviewRecord(
            snapshot.draft.workflow_id, uuid4(), "AWAITING_APPROVAL", JobState.STARTED, snapshot
        )

    async def prepare_review(
        self, snapshot: DraftReview, job_id: UUID, actor_id: UserId
    ) -> ReviewRecord:
        self.calls.append("prepare")
        return self.record

    async def get(self, workflow_id: UUID, actor_id: UserId) -> ReviewRecord:
        self.calls.append("get")
        return self.record

    async def decide(self, command: ApprovalCommand, actor_id: UserId) -> DecisionResult:
        self.calls.append("decide")
        if self.failure:
            raise self.failure
        decision = make_decision(command, self.snapshot, self.actor, uuid4(), NOW)
        return DecisionResult(replace(self.record, decision=decision), replayed=False)


def service_fixture(
    role: Role = Role.REVIEWER,
) -> tuple[ApprovalService, ApprovalStoreSpy, Principal, FakeUserRepository, RecordingAuditSink]:
    user = build_user(user_id=str(uuid4()), email="actor@example.com", role=role)
    actor = Principal.from_user(user)
    snapshot = review_snapshot()
    ownership = FakeOwnershipQuery()
    ownership.register(ResourceType.RUN, str(snapshot.draft.workflow_id), user.id)
    users = FakeUserRepository([user])
    store = ApprovalStoreSpy(snapshot, actor)
    audit = RecordingAuditSink()
    service = ApprovalService(store, users, AuthorizationService(ownership), audit, FixedClock(NOW))
    return service, store, actor, users, audit


async def perform(
    service: ApprovalService, actor: Principal, snapshot: DraftReview, action: ApprovalAction
) -> DecisionResult:
    args = (snapshot.draft.workflow_id, snapshot.draft.draft_id)
    if action == ApprovalAction.APPROVE:
        return await service.approve(*args, actor)
    if action == ApprovalAction.REJECT:
        return await service.reject(*args, REJECTION_REASON, actor)
    return await service.edit_and_approve(*args, EDITED_NOTE, actor)


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("action", list(ApprovalAction))
async def test_server_service_permission_matrix(role: Role, action: ApprovalAction) -> None:
    service, store, actor, _, audit = service_fixture(role)
    if role == Role.ANALYST:
        with pytest.raises(PermissionDeniedError):
            await perform(service, actor, store.snapshot, action)
        assert not store.calls
    else:
        result = await perform(service, actor, store.snapshot, action)
        assert result.review.decision is not None
        assert result.review.decision.actor_id == UUID(actor.user_id.value)
        assert store.calls == ["decide"]
        assert audit.entries[-1].action == "approval." + action.value.lower()


@pytest.mark.parametrize("action", list(ApprovalAction))
async def test_forged_or_stale_principal_role_cannot_approve(action: ApprovalAction) -> None:
    service, store, actor, _, _ = service_fixture(Role.ANALYST)
    forged = replace(actor, role=Role.ADMIN)
    with pytest.raises(PermissionDeniedError):
        await perform(service, forged, store.snapshot, action)
    assert not store.calls


async def test_deleted_actor_is_denied_before_persistence() -> None:
    service, store, actor, _, _ = service_fixture()
    missing = replace(actor, user_id=UserId(str(uuid4())))
    with pytest.raises(UnknownPrincipalError):
        await service.approve(
            store.snapshot.draft.workflow_id, store.snapshot.draft.draft_id, missing
        )
    assert not store.calls


async def test_owner_can_lookup_decision_but_cannot_open_review_screen() -> None:
    service, store, actor, _, _ = service_fixture(Role.ANALYST)
    assert await service.get_decision(store.snapshot.draft.workflow_id, actor) is None
    assert await service.get_finalization_request(store.snapshot.draft.workflow_id, actor) is None
    with pytest.raises(PermissionDeniedError):
        await service.get_review(store.snapshot.draft.workflow_id, actor)


async def test_foreign_analyst_cannot_query_authoritative_decision() -> None:
    service, store, _, users, _ = service_fixture(Role.ANALYST)
    other = build_user(user_id=str(uuid4()), email="other@example.com", role=Role.ANALYST)
    await users.add(other)
    with pytest.raises(ResourceOwnershipError):
        await service.get_decision(store.snapshot.draft.workflow_id, Principal.from_user(other))
    assert not store.calls


async def test_failed_commit_does_not_return_signal_or_log_success() -> None:
    service, store, actor, _, audit = service_fixture()
    store.failure = ApprovalStoreError("storage failure")
    with pytest.raises(ApprovalStoreError):
        await service.approve(
            store.snapshot.draft.workflow_id, store.snapshot.draft.draft_id, actor
        )
    assert not audit.entries


@pytest.mark.parametrize(
    "kind", ["capacity", "refused", "flagged", "unsupported", "tool_error", "timeout"]
)
@pytest.mark.parametrize("action", [ApprovalAction.APPROVE, ApprovalAction.EDIT_AND_APPROVE])
def test_review_only_draft_cannot_be_approved(kind: str, action: ApprovalAction) -> None:
    snapshot = review_snapshot(kind=kind)
    actor = Principal.from_user(
        build_user(user_id=str(uuid4()), email="reviewer@example.com", role=Role.REVIEWER)
    )
    before = asdict(snapshot)
    command = ApprovalCommand(
        snapshot.draft.workflow_id,
        snapshot.draft.draft_id,
        action,
        edited_note=EDITED_NOTE if action == ApprovalAction.EDIT_AND_APPROVE else None,
    )
    assert not draft_is_approvable(snapshot)
    with pytest.raises(InvalidStateTransitionError):
        make_decision(command, snapshot, actor, uuid4(), NOW)
    assert asdict(snapshot) == before
    if kind == "capacity":
        assert snapshot.draft.can_proceed and snapshot.draft.refused


@pytest.mark.parametrize("kind", ["capacity", "flagged", "unsupported", "tool_error", "timeout"])
def test_review_only_draft_may_be_rejected_without_modifying_provenance(kind: str) -> None:
    snapshot = review_snapshot(kind=kind)
    decision = make_decision(
        ApprovalCommand(
            snapshot.draft.workflow_id,
            snapshot.draft.draft_id,
            ApprovalAction.REJECT,
            reason=REJECTION_REASON,
        ),
        snapshot,
        Principal.from_user(
            build_user(user_id=str(uuid4()), email="reviewer@example.com", role=Role.REVIEWER)
        ),
        uuid4(),
        NOW,
    )
    assert decision.status == ApprovalStatus.REJECTED
    assert decision.approved_note is None and decision.original_note == snapshot.draft.note


@pytest.mark.parametrize("reason", [None, "", "  ", "!?!", "a", "a" * 4001])
def test_reject_requires_meaningful_bounded_reason(reason: Any) -> None:
    snapshot = review_snapshot()
    with pytest.raises(InvariantViolationError):
        validate_command(
            ApprovalCommand(
                snapshot.draft.workflow_id,
                snapshot.draft.draft_id,
                ApprovalAction.REJECT,
                reason=reason,
            )
        )


@pytest.mark.parametrize(
    "original,approved",
    [
        ("old\n", "new\n"),
        ("old", "new"),
        ("line", "line\n"),
        ("line\r\n", "line\n"),
        ("مرحبا\n", "مرحبا بالعالم\n"),
    ],
)
def test_diff_records_additions_removals_and_line_ending_changes(
    original: str, approved: str
) -> None:
    difference = textual_diff(original, approved)
    assert difference == textual_diff(original, approved)
    assert difference.startswith("--- original\n+++ approved\n")
    assert "@@" in difference
    assert any(
        line.startswith("-") and not line.startswith("---") for line in difference.splitlines()
    )
    assert any(
        line.startswith("+") and not line.startswith("+++") for line in difference.splitlines()
    )


def test_edit_preserves_original_and_requires_actual_change() -> None:
    snapshot = review_snapshot()
    actor = Principal.from_user(
        build_user(user_id=str(uuid4()), email="reviewer@example.com", role=Role.REVIEWER)
    )
    command = ApprovalCommand(
        snapshot.draft.workflow_id,
        snapshot.draft.draft_id,
        ApprovalAction.EDIT_AND_APPROVE,
        edited_note=EDITED_NOTE,
    )
    result = make_decision(command, snapshot, actor, uuid4(), NOW)
    assert result.original_note == snapshot.draft.note
    assert result.approved_note == EDITED_NOTE and result.diff
    assert result.approved_draft_id != result.draft_id
    with pytest.raises(InvariantViolationError):
        make_decision(
            replace(command, edited_note=snapshot.draft.note), snapshot, actor, uuid4(), NOW
        )


def test_replay_requires_same_actor_action_draft_and_content() -> None:
    snapshot = review_snapshot()
    actor = Principal.from_user(
        build_user(user_id=str(uuid4()), email="reviewer@example.com", role=Role.REVIEWER)
    )
    command = ApprovalCommand(
        snapshot.draft.workflow_id, snapshot.draft.draft_id, ApprovalAction.APPROVE
    )
    decision = make_decision(command, snapshot, actor, uuid4(), NOW)
    assert is_same_decision(decision, command, actor)
    assert not is_same_decision(decision, replace(command, draft_id="0" * 64), actor)
    assert not is_same_decision(decision, replace(command, action=ApprovalAction.REJECT), actor)
    assert not is_same_decision(decision, command, replace(actor, user_id=UserId(str(uuid4()))))


def test_review_registration_cannot_mix_workflows_or_claim_provenance() -> None:
    snapshot = review_snapshot()
    with pytest.raises(InvariantViolationError):
        DraftReview(snapshot.draft, replace(snapshot.safety_verdict, workflow_id=uuid4()))
    claim = replace(snapshot.draft.asserted_claims[0], detail="Unverified replacement")
    with pytest.raises(InvariantViolationError):
        DraftReview(replace(snapshot.draft, asserted_claims=(claim,)), snapshot.safety_verdict)
