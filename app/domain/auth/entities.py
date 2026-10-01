"""The User entity -- a persisted identity and its single role.

Immutable, like :class:`~app.domain.documents.entities.Document`. A user's role
is assigned server-side and is never derived from request content, so there is no
method here to "set" a role from client input.

``__repr__`` is overridden to mask ``hashed_password``. A frozen dataclass's
generated ``repr`` would otherwise print the hash, and reprs end up in log lines,
assertion failures, and traceback frame summaries -- the exact places a credential
digest must never appear.

Pure stdlib + domain value objects, errors, and the permission matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.auth.permissions import role_has_permission, role_may_access_any
from app.domain.auth.value_objects import (
    EmailAddress,
    Permission,
    ResourceType,
    Role,
    UserId,
)
from app.domain.shared.errors import InvariantViolationError


@dataclass(frozen=True, slots=True, repr=False)
class User:
    """A registered user: identity, login email, role, and password digest.

    ``hashed_password`` holds the output of an :class:`IPasswordHasher` -- never a
    plaintext password. The entity does not know or care which algorithm produced
    it; verification goes back through the port.
    """

    id: UserId
    email: EmailAddress
    role: Role
    hashed_password: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.hashed_password, str) or not self.hashed_password.strip():
            raise InvariantViolationError("User.hashed_password must not be empty")

    def has_permission(self, permission: Permission) -> bool:
        """Return whether this user's role grants ``permission`` (BRD AC-8.2)."""
        return role_has_permission(self.role, permission)

    def may_access_any(self, resource_type: ResourceType) -> bool:
        """Return whether this user may access ``resource_type`` objects they do not own."""
        return role_may_access_any(self.role, resource_type)

    def owns(self, owner_id: UserId | None) -> bool:
        """Return whether this user is the owner identified by ``owner_id``.

        A ``None`` owner is never owned by anyone: an object with no recorded
        owner must not become accessible to every caller by default.
        """
        if owner_id is None:
            return False
        return self.id.value == owner_id.value

    def __repr__(self) -> str:
        return (
            f"User(id={self.id.value!r}, email={self.email.value!r}, "
            f"role={self.role.value!r}, hashed_password=***)"
        )
