"""The single place authorization decisions are made (BRD AC-8.3, AC-8.4, SEC-1a).

Every route and use case asks this service; none of them compares a role string.
That is what keeps ``if user.role == "admin"`` from spreading, and it means the
permission matrix can change in one file.

Two kinds of decision, and the difference matters:

* :meth:`require_permission` -- a *capability* check. "May a reviewer approve a
  note?" Answered from the role matrix alone.
* :meth:`require_resource_access` -- an *object* check. "May this reviewer read
  run ``X``?" Answered by reading persisted ownership. A token cannot satisfy
  this check, nor can a UUID being hard to guess: the row is looked up and the
  owner compared (defeating IDOR/BOLA).

This is intentionally a pair of explicit methods rather than a general policy
engine. The matrix has three roles and sixteen permissions; a rules DSL would be
more machinery than the problem has.
"""

from __future__ import annotations

from app.application.auth.context import Principal
from app.application.errors import (
    PermissionDeniedError,
    ResourceNotFoundError,
    ResourceOwnershipError,
)
from app.application.ports.ownership import IOwnershipQuery
from app.domain.auth.permissions import view_permission
from app.domain.auth.value_objects import Permission, ResourceType, UserId


class AuthorizationService:
    """Enforces the role matrix and persisted object ownership."""

    def __init__(self, ownership_query: IOwnershipQuery) -> None:
        self._ownership = ownership_query

    def require_permission(self, principal: Principal, permission: Permission) -> None:
        """Raise unless ``principal``'s role grants ``permission``.

        Raises :class:`PermissionDeniedError` (403). The message names the role
        and permission for the server-side log; the HTTP boundary replaces it
        with a static message before it reaches the client.
        """
        if not principal.has_permission(permission):
            raise PermissionDeniedError(
                f"role {principal.role.value!r} lacks permission {permission.value!r}"
            )

    def is_permitted(self, principal: Principal, permission: Permission) -> bool:
        """Return whether ``principal`` holds ``permission``, without raising.

        For the rare case where a use case varies its behaviour by permission
        (filtering a list, say) instead of refusing the request outright.
        """
        return principal.has_permission(permission)

    async def require_resource_access(
        self,
        principal: Principal,
        resource_type: ResourceType,
        resource_id: str,
    ) -> UserId:
        """Raise unless ``principal`` may access this specific object; return its owner.

        The order of the checks is deliberate:

        1. **Baseline capability.** A principal whose role cannot read this
           resource type at all is refused before any lookup happens, so it
           learns nothing about whether the object exists.
        2. **Existence.** An absent (or unowned) row yields
           :class:`ResourceNotFoundError` (404). A random or malformed id lands
           here, which is why guessing an id cannot bypass anything -- there is
           no owner to match against.
        3. **Ownership.** The owner recorded in the store is compared against the
           principal.
        4. **Documented cross-user grant.** Only then may a reviewer or admin be
           allowed through, and only where the AC-8.2 matrix says so for this
           resource type -- never by being treated as the owner.

        Anything else raises :class:`ResourceOwnershipError` (403).

        Distinguishing 404 (no such object) from 403 (someone else's object) does
        tell a caller whether an id exists. That is the behaviour Ticket #5
        specifies, and it is the conventional one; the alternative -- answering 404
        for both -- would trade a truthful error for a marginal gain, since an id
        is a v4 UUID that an attacker cannot enumerate in the first place.
        """
        required = view_permission(resource_type)
        if required is not None and not principal.has_permission(required):
            raise PermissionDeniedError(
                f"role {principal.role.value!r} cannot read {resource_type.value} objects"
            )

        owner_id = await self._ownership.owner_of(resource_type, resource_id)
        if owner_id is None:
            raise ResourceNotFoundError(f"No {resource_type.value} found for the given id")

        if principal.owns(owner_id):
            return owner_id

        if principal.may_access_any(resource_type):
            return owner_id

        raise ResourceOwnershipError(
            f"role {principal.role.value!r} may not access another user's {resource_type.value}"
        )
