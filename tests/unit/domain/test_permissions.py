"""The BRD AC-8.2 permission matrix (FR-8).

The expected grants are written out literally here rather than derived from
:mod:`app.domain.auth.permissions`. A test that computed them the same way the
implementation does would pass no matter what the implementation said; spelling
them out means this file is a second, independent statement of the matrix, and a
change to one without the other fails.

The source is BRD FR-8 AC-8.2:

    analyst  (PER-01): ask questions, run workflows, view own runs/jobs/traces,
                       view own session history
    reviewer (PER-02): all analyst permissions + approve/reject/edit clinical
                       notes, view pending approvals, view all runs
    admin    (PER-03): all reviewer permissions + ingest documents, manage users,
                       manage all jobs, view system health, view cost dashboard
"""

from __future__ import annotations

import pytest

from app.domain.auth.permissions import (
    ROLE_PERMISSIONS,
    ownership_bypass_permission,
    permissions_for,
    role_has_permission,
    role_may_access_any,
    view_permission,
)
from app.domain.auth.value_objects import Permission, ResourceType, Role

_ANALYST_GRANTS = {
    Permission.ASK_QUESTION,
    Permission.RUN_WORKFLOW,
    Permission.VIEW_OWN_RUNS,
    Permission.VIEW_OWN_JOBS,
    Permission.VIEW_OWN_TRACES,
    Permission.VIEW_OWN_SESSIONS,
}

_REVIEWER_ONLY_GRANTS = {
    Permission.APPROVE_CLINICAL_NOTE,
    Permission.REJECT_CLINICAL_NOTE,
    Permission.EDIT_CLINICAL_NOTE,
    Permission.VIEW_PENDING_APPROVALS,
    Permission.VIEW_ALL_RUNS,
}

_ADMIN_ONLY_GRANTS = {
    Permission.INGEST_DOCUMENTS,
    Permission.MANAGE_USERS,
    Permission.MANAGE_ALL_JOBS,
    Permission.VIEW_SYSTEM_HEALTH,
    Permission.VIEW_COST_DASHBOARD,
}

_EXPECTED_MATRIX = {
    Role.ANALYST: _ANALYST_GRANTS,
    Role.REVIEWER: _ANALYST_GRANTS | _REVIEWER_ONLY_GRANTS,
    Role.ADMIN: _ANALYST_GRANTS | _REVIEWER_ONLY_GRANTS | _ADMIN_ONLY_GRANTS,
}


class TestMatrixCompleteness:
    def test_every_role_has_an_entry(self) -> None:
        # A role with no entry would raise KeyError inside an authorization check,
        # which would surface as a 500 rather than a decision.
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_every_permission_is_granted_to_at_least_one_role(self) -> None:
        granted = set().union(*ROLE_PERMISSIONS.values())
        assert granted == set(Permission), "unreachable permissions: " + str(
            set(Permission) - granted
        )

    @pytest.mark.parametrize("role", list(Role))
    def test_grants_match_the_brd_exactly(self, role: Role) -> None:
        assert permissions_for(role) == _EXPECTED_MATRIX[role]


class TestCumulativeGrants:
    def test_reviewer_has_every_analyst_permission(self) -> None:
        # AC-8.2 words reviewer as "all analyst permissions + ...".
        assert permissions_for(Role.ANALYST) <= permissions_for(Role.REVIEWER)

    def test_admin_has_every_reviewer_permission(self) -> None:
        assert permissions_for(Role.REVIEWER) <= permissions_for(Role.ADMIN)

    def test_the_tiers_are_strictly_increasing(self) -> None:
        assert len(permissions_for(Role.ANALYST)) < len(permissions_for(Role.REVIEWER))
        assert len(permissions_for(Role.REVIEWER)) < len(permissions_for(Role.ADMIN))


class TestRoleDenials:
    @pytest.mark.parametrize("permission", sorted(_REVIEWER_ONLY_GRANTS))
    def test_analyst_is_denied_reviewer_permissions(self, permission: Permission) -> None:
        assert not role_has_permission(Role.ANALYST, permission)

    @pytest.mark.parametrize("permission", sorted(_ADMIN_ONLY_GRANTS))
    def test_analyst_is_denied_admin_permissions(self, permission: Permission) -> None:
        assert not role_has_permission(Role.ANALYST, permission)

    @pytest.mark.parametrize("permission", sorted(_ADMIN_ONLY_GRANTS))
    def test_reviewer_is_denied_admin_permissions(self, permission: Permission) -> None:
        assert not role_has_permission(Role.REVIEWER, permission)

    @pytest.mark.parametrize("permission", sorted(_ANALYST_GRANTS))
    def test_analyst_is_allowed_its_own_permissions(self, permission: Permission) -> None:
        assert role_has_permission(Role.ANALYST, permission)

    @pytest.mark.parametrize("permission", sorted(_REVIEWER_ONLY_GRANTS))
    def test_reviewer_is_allowed_reviewer_permissions(self, permission: Permission) -> None:
        assert role_has_permission(Role.REVIEWER, permission)

    @pytest.mark.parametrize("permission", sorted(_ADMIN_ONLY_GRANTS))
    def test_admin_is_allowed_admin_permissions(self, permission: Permission) -> None:
        assert role_has_permission(Role.ADMIN, permission)


class TestOwnershipBypass:
    """Who may read an object belonging to somebody else, per resource type."""

    #: Expected cross-user access. Follows the specific AC-8.2 grants rather than
    #: AC-8.4's broader "unless reviewer/admin" parenthetical -- see the
    #: permissions module docstring for why, and note that an analyst is denied in
    #: every row either way, which is what AC-8.4 actually requires.
    _EXPECTED = {
        ResourceType.RUN: {Role.REVIEWER, Role.ADMIN},  # "view all runs"
        ResourceType.JOB: {Role.ADMIN},  # "manage all jobs"
        ResourceType.TRACE: {Role.ADMIN},  # "view system health/observability"
        ResourceType.SESSION: set(),  # nothing widens "own session history"
        ResourceType.DOCUMENT: {Role.ADMIN},  # "ingest documents" (SEC-1a)
    }

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    def test_cross_user_access_matches_the_matrix(self, resource_type: ResourceType) -> None:
        allowed = {role for role in Role if role_may_access_any(role, resource_type)}
        assert allowed == self._EXPECTED[resource_type]

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    def test_analyst_can_never_reach_another_users_object(
        self, resource_type: ResourceType
    ) -> None:
        # BRD AC-8.4, stated directly.
        assert not role_may_access_any(Role.ANALYST, resource_type)

    def test_session_history_is_private_from_every_role(self) -> None:
        assert ownership_bypass_permission(ResourceType.SESSION) is None
        for role in Role:
            assert not role_may_access_any(role, ResourceType.SESSION)

    def test_every_resource_type_has_a_bypass_rule(self) -> None:
        # A missing entry would raise KeyError mid-authorization.
        for resource_type in ResourceType:
            ownership_bypass_permission(resource_type)


class TestBaselineViewPermission:
    _EXPECTED = {
        ResourceType.RUN: Permission.VIEW_OWN_RUNS,
        ResourceType.JOB: Permission.VIEW_OWN_JOBS,
        ResourceType.TRACE: Permission.VIEW_OWN_TRACES,
        ResourceType.SESSION: Permission.VIEW_OWN_SESSIONS,
        ResourceType.DOCUMENT: None,
    }

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    def test_baseline_permission_per_resource_type(self, resource_type: ResourceType) -> None:
        assert view_permission(resource_type) == self._EXPECTED[resource_type]

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    def test_every_role_holds_the_baseline_today(self, resource_type: ResourceType) -> None:
        # The four view-own grants are in the analyst tier, which the other roles
        # inherit, so the baseline check never refuses anyone at present. It is
        # still enforced; this records the current state so a future narrowing of
        # a role shows up here.
        required = view_permission(resource_type)
        if required is None:
            pytest.skip(f"{resource_type.value} has no baseline view permission")
        for role in Role:
            assert role_has_permission(role, required)


class TestMatrixImmutability:
    def test_grant_sets_cannot_be_mutated(self) -> None:
        # frozenset, not set: a caller cannot widen its own permissions by
        # mutating the shared matrix.
        grants = permissions_for(Role.ANALYST)
        assert isinstance(grants, frozenset)
        with pytest.raises(AttributeError):
            grants.add(Permission.MANAGE_USERS)  # type: ignore[attr-defined]
