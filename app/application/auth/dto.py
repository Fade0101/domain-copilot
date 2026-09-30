"""Output DTOs (read models) for the auth use cases.

These are the shapes the presentation layer is allowed to serialize. Neither one
has a ``hashed_password`` field, so "the API must never return a password hash"
is enforced by the type rather than by remembering to omit it in each response
schema -- there is nothing to omit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.application.auth.context import Principal
from app.application.ports.tokens import IssuedToken


@dataclass(frozen=True, slots=True)
class PrincipalView:
    """Read model describing the authenticated caller.

    ``permissions`` is the resolved AC-8.2 grant set, sorted for a stable
    response body. Exposing it lets a UI hide controls it must not offer, while
    the server still enforces every one of them on the request that follows
    (BRD AC-8.3 -- permissions are enforced server-side, not by hiding buttons).
    """

    id: str
    email: str
    role: str
    permissions: tuple[str, ...]

    @classmethod
    def from_principal(cls, principal: Principal) -> PrincipalView:
        """Project a :class:`Principal` into a serializable view."""
        return cls(
            id=principal.user_id.value,
            email=principal.email.value,
            role=principal.role.value,
            permissions=tuple(sorted(p.value for p in principal.permissions())),
        )


@dataclass(frozen=True, slots=True, repr=False)
class AccessTokenView:
    """An issued bearer token, in the shape a client needs to use it.

    Masked ``__repr__``: the one field here *is* a live credential.
    """

    access_token: str
    token_type: str
    expires_in: int
    expires_at: datetime

    @classmethod
    def from_issued_token(cls, token: IssuedToken) -> AccessTokenView:
        """Project an :class:`IssuedToken` into a response view."""
        return cls(
            access_token=token.access_token,
            token_type=token.token_type,
            expires_in=token.expires_in_seconds,
            expires_at=token.expires_at,
        )

    def __repr__(self) -> str:
        return (
            f"AccessTokenView(access_token=***, token_type={self.token_type!r}, "
            f"expires_in={self.expires_in!r}, expires_at={self.expires_at!r})"
        )
