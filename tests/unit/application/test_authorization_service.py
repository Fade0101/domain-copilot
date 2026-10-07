"""The authorization service: capability checks and persisted-ownership checks.

The ownership tests here are the unit-level counterpart to
``tests/integration/test_ownership_api.py``. They cover the decision logic --
including the *order* of the checks, which determines how much an unauthorized
caller can learn -- while the integration tests cover it over real HTTP.
"""

from __future__ import annotations

import pytest

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import (
    PermissionDeniedError,
    ResourceNotFoundError,
    ResourceOwnershipError,
)
from app.domain.auth.value_objects import Permission, ResourceType, Role
from tests.support.fakes import FakeOwnershipQuery, build_user

_OWNER_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
_OTHER_ID = "11111111-1111-4111-8111-111111111111"
_RESOURCE_ID = "8a7b6c5d-4e3f-4a1b-8c9d-0e1f2a3b4c5d"


def _principal(role: Role, user_id: str = _OWNER_ID) -> Principal:
    return Principal.from_user(
        build_user(user_id=user_id, email=f"{role.value}@example.com", role=role)
    )


@pytest.fixture
def ownership() -> FakeOwnershipQuery:
    return FakeOwnershipQuery()


@pytest.fixture
def service(ownership: FakeOwnershipQuery) -> AuthorizationService:
    return AuthorizationService(ownership_query=ownership)


class TestRequirePermission:
    def test_allows_a_role_that_holds_the_permission(self, service: AuthorizationService) -> None:
        service.require_permission(_principal(Role.ADMIN), Permission.MANAGE_USERS)

    def test_refuses_a_role_that_does_not(self, service: AuthorizationService) -> None:
        with pytest.raises(PermissionDeniedError):
            service.require_permission(_principal(Role.ANALYST), Permission.MANAGE_USERS)

    def test_refuses_a_reviewer_an_admin_only_permission(
        self, service: AuthorizationService
    ) -> None:
        with pytest.raises(PermissionDeniedError):
            service.require_permission(_principal(Role.REVIEWER), Permission.INGEST_DOCUMENTS)

    def test_is_permitted_answers_without_raising(self, service: AuthorizationService) -> None:
        assert service.is_permitted(_principal(Role.ADMIN), Permission.MANAGE_USERS)
        assert not service.is_permitted(_principal(Role.ANALYST), Permission.MANAGE_USERS)


class TestOwnershipOfOwnObjects:
    @pytest.mark.parametrize("resource_type", list(ResourceType))
    async def test_the_owner_may_always_access_its_own_object(
        self,
        service: AuthorizationService,
        ownership: FakeOwnershipQuery,
        resource_type: ResourceType,
    ) -> None:
        principal = _principal(Role.ANALYST)
        ownership.register(resource_type, _RESOURCE_ID, principal.user_id)
        owner = await service.require_resource_access(
            principal=principal, resource_type=resource_type, resource_id=_RESOURCE_ID
        )
        assert owner.value == _OWNER_ID


#: Which roles may read an object of each type that they do not own. Module level
#: because mypy cannot resolve a class attribute referenced from a decorator.
_EXPECT_ALLOWED: dict[ResourceType, set[Role]] = {
    ResourceType.RUN: {Role.REVIEWER, Role.ADMIN},
    ResourceType.JOB: {Role.ADMIN},
    ResourceType.TRACE: {Role.ADMIN},
    ResourceType.SESSION: set(),
}

#: Listed explicitly rather than derived: ``parametrize`` takes ``Iterable[object]``,
#: which would make a ``sorted(..., key=...)`` lambda's argument ``object``.
_OWNED_RESOURCE_TYPES = [
    ResourceType.JOB,
    ResourceType.RUN,
    ResourceType.SESSION,
    ResourceType.TRACE,
]


class TestOwnershipOfOtherUsersObjects:
    """Cross-user access follows the matrix, per resource type."""

    @pytest.mark.parametrize("role", list(Role))
    @pytest.mark.parametrize("resource_type", _OWNED_RESOURCE_TYPES)
    async def test_cross_user_access_matches_the_matrix(
        self,
        service: AuthorizationService,
        ownership: FakeOwnershipQuery,
        resource_type: ResourceType,
        role: Role,
    ) -> None:
        # The object belongs to somebody else in every case.
        ownership.register(resource_type, _RESOURCE_ID, _principal(Role.ANALYST).user_id)
        caller = _principal(role, user_id=_OTHER_ID)

        if role in _EXPECT_ALLOWED[resource_type]:
            owner = await service.require_resource_access(
                principal=caller, resource_type=resource_type, resource_id=_RESOURCE_ID
            )
            # Allowed through a documented grant -- not by being mistaken for the
            # owner. The owner reported back is still the real one.
            assert owner.value == _OWNER_ID
        else:
            with pytest.raises(ResourceOwnershipError):
                await service.require_resource_access(
                    principal=caller, resource_type=resource_type, resource_id=_RESOURCE_ID
                )

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    async def test_an_analyst_never_reaches_another_analysts_object(
        self,
        service: AuthorizationService,
        ownership: FakeOwnershipQuery,
        resource_type: ResourceType,
    ) -> None:
        ownership.register(resource_type, _RESOURCE_ID, _principal(Role.ANALYST).user_id)
        with pytest.raises(ResourceOwnershipError):
            await service.require_resource_access(
                principal=_principal(Role.ANALYST, user_id=_OTHER_ID),
                resource_type=resource_type,
                resource_id=_RESOURCE_ID,
            )

    async def test_even_an_admin_cannot_read_another_users_session(
        self, service: AuthorizationService, ownership: FakeOwnershipQuery
    ) -> None:
        # AC-8.2 grants nobody access to another user's session history.
        ownership.register(ResourceType.SESSION, _RESOURCE_ID, _principal(Role.ANALYST).user_id)
        with pytest.raises(ResourceOwnershipError):
            await service.require_resource_access(
                principal=_principal(Role.ADMIN, user_id=_OTHER_ID),
                resource_type=ResourceType.SESSION,
                resource_id=_RESOURCE_ID,
            )


class TestUnknownObjects:
    @pytest.mark.parametrize("resource_type", list(ResourceType))
    @pytest.mark.parametrize("role", list(Role))
    async def test_an_unknown_id_is_never_a_bypass(
        self,
        service: AuthorizationService,
        resource_type: ResourceType,
        role: Role,
    ) -> None:
        # Nothing is registered, so there is no owner to match. For every role and
        # every resource type this must refuse rather than fall through to allow.
        with pytest.raises((ResourceNotFoundError, PermissionDeniedError)):
            await service.require_resource_access(
                principal=_principal(role),
                resource_type=resource_type,
                resource_id="00000000-0000-4000-8000-000000000000",
            )

    @pytest.mark.parametrize(
        "resource_id", ["", "   ", "not-a-uuid", "../../etc/passwd", "1 OR 1=1", "%00"]
    )
    async def test_a_malformed_id_is_a_miss_not_an_error(
        self, service: AuthorizationService, resource_id: str
    ) -> None:
        # A hostile id must reach the same refusal path as any other unknown id,
        # not raise out of the authorization check as a 500.
        with pytest.raises(ResourceNotFoundError):
            await service.require_resource_access(
                principal=_principal(Role.ADMIN),
                resource_type=ResourceType.RUN,
                resource_id=resource_id,
            )

    async def test_an_unowned_object_is_not_accessible(
        self, service: AuthorizationService, ownership: FakeOwnershipQuery
    ) -> None:
        # owner_of returning None covers both "no such row" and "row with no
        # owner". Neither may result in access.
        assert await ownership.owner_of(ResourceType.RUN, _RESOURCE_ID) is None
        with pytest.raises(ResourceNotFoundError):
            await service.require_resource_access(
                principal=_principal(Role.ADMIN),
                resource_type=ResourceType.RUN,
                resource_id=_RESOURCE_ID,
            )


class TestCheckOrdering:
    async def test_a_caller_without_baseline_permission_triggers_no_lookup(
        self, ownership: FakeOwnershipQuery
    ) -> None:
        # Ordering matters for disclosure: a principal that may not read this
        # resource type at all must be refused before the store is consulted, so
        # it cannot infer from the response whether the object exists.
        class NoViewPrincipal(Principal):
            def has_permission(self, permission: Permission) -> bool:
                return False

        base = _principal(Role.ANALYST)
        stripped = NoViewPrincipal(user_id=base.user_id, email=base.email, role=base.role)
        service = AuthorizationService(ownership_query=ownership)
        ownership.register(ResourceType.RUN, _RESOURCE_ID, base.user_id)

        with pytest.raises(PermissionDeniedError):
            await service.require_resource_access(
                principal=stripped,
                resource_type=ResourceType.RUN,
                resource_id=_RESOURCE_ID,
            )
        assert ownership.lookups == []

    async def test_the_decision_consults_persisted_ownership(
        self, service: AuthorizationService, ownership: FakeOwnershipQuery
    ) -> None:
        # The check must read the store, not the principal's own claims about
        # what it owns.
        principal = _principal(Role.ANALYST)
        ownership.register(ResourceType.JOB, _RESOURCE_ID, principal.user_id)
        await service.require_resource_access(
            principal=principal, resource_type=ResourceType.JOB, resource_id=_RESOURCE_ID
        )
        assert ownership.lookups == [("job", _RESOURCE_ID)]
