"""Request identity: the typed contracts every layer above the domain speaks.

Two types, with different jobs:

* :class:`Principal` is the *authoritative* in-process identity. It exists only
  after a token has been validated **and** the named user loaded from the store,
  so holding one is proof that authentication succeeded. Use cases take it as a
  parameter; they never accept a role or a user id as a loose string.
* :class:`ExecutionContext` is the *serializable* envelope that carries identity
  across a process boundary -- into a Celery job, a workflow run, an agent or
  tool invocation, an audit record. It is primitives only, so it survives a JSON
  round-trip through a queue message.

Neither type has a field for a token or a password hash. Identity propagates;
credentials do not. A worker that needs to make a *fresh* authorization decision
re-resolves the user from the store rather than trusting the role it was handed,
because a context that has been sitting in a queue may be describing a role the
user no longer has.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.auth.entities import User
from app.domain.auth.permissions import (
    permissions_for,
    role_has_permission,
    role_may_access_any,
)
from app.domain.auth.value_objects import (
    EmailAddress,
    Permission,
    ResourceType,
    Role,
    UserId,
)

_CONTEXT_USER_ID_KEY = "user_id"
_CONTEXT_ROLE_KEY = "role"
_CONTEXT_CORRELATION_KEY = "correlation_id"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor of a request.

    Constructed only by :meth:`from_user`, from a record loaded out of the user
    store. There is deliberately no constructor that takes a role from request
    content: privilege escalation by sending ``{"role": "admin"}`` is impossible
    because no code path turns client input into a ``Principal``.
    """

    user_id: UserId
    email: EmailAddress
    role: Role

    @classmethod
    def from_user(cls, user: User) -> Principal:
        """Project a persisted :class:`User` into the request-scoped principal.

        The role comes from the stored record, which is what makes it the
        server-side authority rather than a claim the caller presented.
        """
        return cls(user_id=user.id, email=user.email, role=user.role)

    def has_permission(self, permission: Permission) -> bool:
        """Return whether this principal's role grants ``permission`` (BRD AC-8.2)."""
        return role_has_permission(self.role, permission)

    def may_access_any(self, resource_type: ResourceType) -> bool:
        """Return whether this principal may read ``resource_type`` objects it does not own."""
        return role_may_access_any(self.role, resource_type)

    def owns(self, owner_id: UserId | None) -> bool:
        """Return whether this principal is the owner identified by ``owner_id``.

        An unowned object (``None``) is owned by nobody, so this is ``False``.
        """
        if owner_id is None:
            return False
        return self.user_id.value == owner_id.value

    def permissions(self) -> frozenset[Permission]:
        """Return every permission this principal's role grants."""
        return permissions_for(self.role)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Serializable identity for work that outlives the HTTP request.

    Primitives only: this is what gets embedded in a queue message, a workflow
    run record, or a trace so that later tickets (#17 observability, #19
    approvals, #23 tracing) can attribute work to the user who started it.
    """

    user_id: str
    role: str
    correlation_id: str | None = None

    @classmethod
    def from_principal(
        cls, principal: Principal, correlation_id: str | None = None
    ) -> ExecutionContext:
        """Build a propagation envelope for ``principal``."""
        return cls(
            user_id=principal.user_id.value,
            role=principal.role.value,
            correlation_id=correlation_id,
        )

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable mapping for embedding in a message payload."""
        return {
            _CONTEXT_USER_ID_KEY: self.user_id,
            _CONTEXT_ROLE_KEY: self.role,
            _CONTEXT_CORRELATION_KEY: self.correlation_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, str | None]) -> ExecutionContext:
        """Rebuild a context from :meth:`to_dict` output.

        Raises :class:`ValueError` when a required key is absent. This is an
        internal round-trip of our own payloads, not client input, so a missing
        key is a bug in the producer rather than a request to reject.
        """
        user_id = payload.get(_CONTEXT_USER_ID_KEY)
        role = payload.get(_CONTEXT_ROLE_KEY)
        if not user_id or not role:
            raise ValueError("ExecutionContext payload is missing user_id or role")
        return cls(
            user_id=user_id,
            role=role,
            correlation_id=payload.get(_CONTEXT_CORRELATION_KEY),
        )

    def owner_id(self) -> UserId:
        """Return the propagated identity as a validated :class:`UserId`."""
        return UserId(self.user_id)

    def as_role(self) -> Role:
        """Return the propagated role as a :class:`Role`.

        For display and audit only. A worker making a live authorization decision
        must reload the user instead; see the module docstring.
        """
        return Role(self.role)
