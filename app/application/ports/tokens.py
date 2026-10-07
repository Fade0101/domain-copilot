"""Access-token port (BRD AC-8.1, SDD A.5.1 "Auth middleware").

JWT encoding, signing, and claim validation are infrastructure concerns; the
application layer only asks for a token to be issued or verified. No JWT library
is importable from domain or application code.

The port speaks in plain strings and ``datetime`` rather than domain value
objects on purpose: a token arriving from a client may carry a subject that is
not a valid UUID or a role that is not one of ours. Those are *authentication*
failures (401), not domain invariant violations (422), so the application maps
raw claims to :class:`UserId`/:class:`Role` itself and raises the right error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

#: The only token type this system issues. A token presenting anything else is
#: rejected, so a future refresh token can never be replayed as an access token.
ACCESS_TOKEN_TYPE = "access"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The validated claims of an access token.

    An instance exists only if the signature, expiry, issuer, audience, token
    type, and required-claim checks all passed.
    """

    subject: str
    role: str
    issued_at: datetime
    expires_at: datetime
    token_type: str = ACCESS_TOKEN_TYPE


@dataclass(frozen=True, slots=True, repr=False)
class IssuedToken:
    """A freshly minted access token and the metadata a client needs to use it.

    ``__repr__`` is masked: this object holds a live bearer credential and must
    not be reproduced into a log line or a traceback frame summary.
    """

    access_token: str
    expires_at: datetime
    expires_in_seconds: int
    token_type: str = "bearer"

    def __repr__(self) -> str:
        return (
            f"IssuedToken(access_token=***, expires_at={self.expires_at!r}, "
            f"expires_in_seconds={self.expires_in_seconds!r}, "
            f"token_type={self.token_type!r})"
        )


class ITokenService(Protocol):
    """Issues and validates signed, expiring access tokens."""

    def issue(self, *, subject: str, role: str) -> IssuedToken:
        """Mint a signed access token for ``subject`` carrying ``role``.

        The implementation sets issued-at and expiry from configuration; callers
        do not choose the lifetime.
        """
        ...

    def verify(self, token: str) -> TokenClaims:
        """Validate ``token`` and return its claims.

        Raises :class:`~app.application.errors.ExpiredTokenError` when the token
        is past its expiry and
        :class:`~app.application.errors.InvalidTokenError` when it is malformed,
        incorrectly signed, issued for another issuer/audience, of the wrong
        token type, or missing a required claim. The raised error must not embed
        the underlying library's exception text or any part of the token.
        """
        ...
