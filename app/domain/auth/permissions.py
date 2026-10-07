"""The BRD AC-8.2 permission matrix and the ownership-bypass rules (BRD AC-8.4).

This module is the single authoritative encoding of *who can do what*. Routes and
use cases ask questions of it; they never compare role strings themselves, which
is what keeps ``if user.role == "admin"`` from spreading through the codebase.

Role grants are **cumulative**, exactly as AC-8.2 words them: reviewer is "all
analyst permissions + ...", admin is "all reviewer permissions + ...". Building
the sets by union rather than by hand means a permission added to analyst cannot
be accidentally withheld from reviewer and admin.

Ownership bypass
----------------
Owning an object always grants access. The interesting question is who may read
*someone else's* object, and the two relevant BRD clauses are not equally
specific:

* **AC-8.2** (the detailed matrix) grants "view all runs" to reviewer and admin,
  and "manage all jobs" to admin only. It grants no role access to another user's
  sessions, and gives admin observability via "view system health/observability".
* **AC-8.4** (the one-line summary) says an analyst cannot reach another user's
  runs or jobs "unless reviewer/admin".

Read literally, AC-8.4's parenthetical would hand reviewer access to every user's
jobs, which the AC-8.2 matrix does not grant. This module follows the *more
specific and more restrictive* AC-8.2 matrix per resource type. That fully
satisfies AC-8.4's actual requirement -- an analyst is denied in every case -- so
no requirement is weakened; the tighter reading simply refuses to invent a grant
the matrix never made. The divergence is called out here because it is a genuine
ambiguity in a frozen document rather than an implementation preference.

Pure stdlib + domain value objects. No imports beyond the domain.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.auth.value_objects import Permission, ResourceType, Role

# --- AC-8.2, tier by tier ---------------------------------------------------

_ANALYST_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.ASK_QUESTION,
        Permission.RUN_WORKFLOW,
        Permission.VIEW_OWN_RUNS,
        Permission.VIEW_OWN_JOBS,
        Permission.VIEW_OWN_TRACES,
        Permission.VIEW_OWN_SESSIONS,
    }
)

# "All analyst permissions + ..."
_REVIEWER_PERMISSIONS: frozenset[Permission] = _ANALYST_PERMISSIONS | frozenset(
    {
        Permission.APPROVE_CLINICAL_NOTE,
        Permission.REJECT_CLINICAL_NOTE,
        Permission.EDIT_CLINICAL_NOTE,
        Permission.VIEW_PENDING_APPROVALS,
        Permission.VIEW_ALL_RUNS,
    }
)

# "All reviewer permissions + ..."
_ADMIN_PERMISSIONS: frozenset[Permission] = _REVIEWER_PERMISSIONS | frozenset(
    {
        Permission.INGEST_DOCUMENTS,
        Permission.MANAGE_USERS,
        Permission.MANAGE_ALL_JOBS,
        Permission.VIEW_SYSTEM_HEALTH,
        Permission.VIEW_COST_DASHBOARD,
    }
)

#: The permission matrix. Every :class:`Role` member has an entry.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ANALYST: _ANALYST_PERMISSIONS,
    Role.REVIEWER: _REVIEWER_PERMISSIONS,
    Role.ADMIN: _ADMIN_PERMISSIONS,
}

# --- Cross-user access, per resource type (see the module docstring) --------
#
# The permission a role must hold to read an object it does NOT own. ``None``
# means no role may: sessions are "view own session history" for every role in
# AC-8.2, and nothing in the matrix widens that, so a user's session history stays
# private even from an admin. Escalating that is a product decision for a later
# ticket, not a silent default here.
_OWNERSHIP_BYPASS: Mapping[ResourceType, Permission | None] = {
    ResourceType.RUN: Permission.VIEW_ALL_RUNS,  # reviewer + admin
    ResourceType.JOB: Permission.MANAGE_ALL_JOBS,  # admin only
    ResourceType.TRACE: Permission.VIEW_SYSTEM_HEALTH,  # admin only (observability)
    ResourceType.SESSION: None,  # nobody
    ResourceType.DOCUMENT: Permission.INGEST_DOCUMENTS,  # admin only (SEC-1a)
}

# --- Baseline read access, per resource type --------------------------------
#
# The permission a role must hold to read an object it *does* own. Every role
# currently holds all four (they are in the analyst base tier, which reviewer and
# admin inherit), so this check passes for every principal today. It is enforced
# anyway: it makes the authorization decision explicit and server-side rather
# than implied, and it is the check that would start failing -- correctly -- if a
# future ticket ever narrows a role's baseline grants.
#
# ``None`` means the resource type has no baseline view permission in the AC-8.2
# matrix, so ownership alone governs.
_RESOURCE_VIEW_PERMISSION: Mapping[ResourceType, Permission | None] = {
    ResourceType.RUN: Permission.VIEW_OWN_RUNS,
    ResourceType.JOB: Permission.VIEW_OWN_JOBS,
    ResourceType.TRACE: Permission.VIEW_OWN_TRACES,
    ResourceType.SESSION: Permission.VIEW_OWN_SESSIONS,
    ResourceType.DOCUMENT: None,
}


def permissions_for(role: Role) -> frozenset[Permission]:
    """Return every permission granted to ``role``."""
    return ROLE_PERMISSIONS[role]


def role_has_permission(role: Role, permission: Permission) -> bool:
    """Return whether ``role`` is granted ``permission`` by the AC-8.2 matrix."""
    return permission in ROLE_PERMISSIONS[role]


def ownership_bypass_permission(resource_type: ResourceType) -> Permission | None:
    """Return the permission needed to access another user's ``resource_type``.

    ``None`` means cross-user access is denied to every role.
    """
    return _OWNERSHIP_BYPASS[resource_type]


def role_may_access_any(role: Role, resource_type: ResourceType) -> bool:
    """Return whether ``role`` may access ``resource_type`` objects it does not own."""
    required = _OWNERSHIP_BYPASS[resource_type]
    if required is None:
        return False
    return role_has_permission(role, required)


def view_permission(resource_type: ResourceType) -> Permission | None:
    """Return the permission needed to read an owned ``resource_type`` object.

    ``None`` means the matrix defines no baseline permission for that type and
    ownership alone decides.
    """
    return _RESOURCE_VIEW_PERMISSION[resource_type]
