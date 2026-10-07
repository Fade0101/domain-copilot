"""Identity propagation: Principal and ExecutionContext (FR-8 §8).

``ExecutionContext`` is the contract tickets #17/#19/#20-23 will carry into jobs,
workflow runs, traces and agent/tool invocations. What matters now is that it
round-trips through a JSON-shaped payload without losing identity and without
picking up a credential, and that a ``Principal`` cannot be built out of request
content.
"""

from __future__ import annotations

import json

import pytest

from app.application.auth.context import ExecutionContext, Principal
from app.application.auth.dto import PrincipalView
from app.domain.auth.value_objects import Permission, ResourceType, Role, UserId
from tests.support.fakes import build_user

_USER_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _principal(role: Role = Role.ANALYST) -> Principal:
    return Principal.from_user(build_user(user_id=_USER_ID, email="analyst@example.com", role=role))


class TestPrincipal:
    def test_is_built_from_the_stored_user_record(self) -> None:
        user = build_user(user_id=_USER_ID, email="Analyst@Example.com", role=Role.REVIEWER)
        principal = Principal.from_user(user)
        assert principal.user_id == user.id
        assert principal.email.value == "analyst@example.com"
        assert principal.role is Role.REVIEWER

    def test_carries_no_credential(self) -> None:
        principal = _principal()
        assert not hasattr(principal, "hashed_password")
        assert "hash" not in repr(principal).lower()

    def test_delegates_permission_questions_to_the_matrix(self) -> None:
        assert _principal(Role.ADMIN).has_permission(Permission.MANAGE_USERS)
        assert not _principal(Role.ANALYST).has_permission(Permission.MANAGE_USERS)

    def test_may_access_any_follows_the_matrix(self) -> None:
        assert _principal(Role.REVIEWER).may_access_any(ResourceType.RUN)
        assert not _principal(Role.REVIEWER).may_access_any(ResourceType.JOB)
        assert not _principal(Role.ADMIN).may_access_any(ResourceType.SESSION)

    def test_owns_only_its_own_id(self) -> None:
        principal = _principal()
        assert principal.owns(UserId(_USER_ID))
        assert not principal.owns(UserId("11111111-1111-4111-8111-111111111111"))
        assert not principal.owns(None)

    def test_is_immutable(self) -> None:
        principal = _principal()
        with pytest.raises(Exception):
            principal.role = Role.ADMIN  # type: ignore[misc]

    def test_permissions_returns_the_full_grant_set(self) -> None:
        assert Permission.ASK_QUESTION in _principal(Role.ANALYST).permissions()
        assert len(_principal(Role.ADMIN).permissions()) > len(
            _principal(Role.ANALYST).permissions()
        )


class TestExecutionContext:
    def test_captures_the_principal_identity(self) -> None:
        context = ExecutionContext.from_principal(_principal(Role.REVIEWER))
        assert context.user_id == _USER_ID
        assert context.role == "reviewer"
        assert context.correlation_id is None

    def test_carries_a_correlation_id_when_given_one(self) -> None:
        context = ExecutionContext.from_principal(_principal(), correlation_id="corr-1")
        assert context.correlation_id == "corr-1"

    def test_round_trips_through_a_json_payload(self) -> None:
        # The realistic path: serialized into a Celery message, deserialized by a
        # worker in another process.
        original = ExecutionContext.from_principal(_principal(Role.ADMIN), "corr-9")
        revived = ExecutionContext.from_dict(json.loads(json.dumps(original.to_dict())))
        assert revived == original

    def test_payload_is_primitives_only(self) -> None:
        payload = ExecutionContext.from_principal(_principal()).to_dict()
        assert all(value is None or isinstance(value, str) for value in payload.values())

    def test_payload_carries_no_credential(self) -> None:
        # Identity propagates; credentials do not. No token, password, or hash.
        payload = ExecutionContext.from_principal(_principal()).to_dict()
        assert set(payload) == {"user_id", "role", "correlation_id"}

    def test_exposes_identity_as_validated_value_objects(self) -> None:
        context = ExecutionContext.from_principal(_principal(Role.REVIEWER))
        assert context.owner_id() == UserId(_USER_ID)
        assert context.as_role() is Role.REVIEWER

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"role": "admin"},
            {"user_id": _USER_ID},
            {"user_id": "", "role": "admin"},
            {"user_id": _USER_ID, "role": ""},
        ],
    )
    def test_an_incomplete_payload_is_rejected(self, payload: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            ExecutionContext.from_dict(payload)

    def test_is_immutable(self) -> None:
        context = ExecutionContext.from_principal(_principal())
        with pytest.raises(Exception):
            context.role = "admin"  # type: ignore[misc]


class TestPrincipalView:
    def test_projects_identity_role_and_permissions(self) -> None:
        view = PrincipalView.from_principal(_principal(Role.REVIEWER))
        assert view.id == _USER_ID
        assert view.email == "analyst@example.com"
        assert view.role == "reviewer"
        assert Permission.VIEW_ALL_RUNS.value in view.permissions

    def test_permissions_are_sorted_for_a_stable_response(self) -> None:
        view = PrincipalView.from_principal(_principal(Role.ADMIN))
        assert list(view.permissions) == sorted(view.permissions)

    def test_has_no_field_for_a_password_hash(self) -> None:
        # The guarantee is structural: there is no field to forget to omit.
        view = PrincipalView.from_principal(_principal())
        assert not hasattr(view, "hashed_password")
        assert "hash" not in repr(view).lower()
