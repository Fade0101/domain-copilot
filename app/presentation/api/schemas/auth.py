"""Request/response schemas for the auth API.

Pydantic at the presentation edge only, as in :mod:`.documents`.

No schema here has a ``hashed_password`` field, and the application views they are
built from do not either, so a password digest cannot reach a response body by
oversight -- there is no field for one to land in.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.application.auth.dto import AccessTokenView, PrincipalView


class TokenRequest(BaseModel):
    """Body for ``POST /api/v1/auth/token``.

    Extra fields are ignored rather than rejected, which is pydantic's default and
    is kept on purpose: a client that submits ``{"role": "admin"}`` alongside its
    credentials gets a token for whatever role its account actually has. The
    request simply has no field through which a role could be requested, so there
    is nothing to escalate. ``tests/integration/test_auth_api.py`` asserts exactly
    this.
    """

    email: str = Field(min_length=3, max_length=255, examples=["analyst@example.com"])
    # No length bounds beyond a sane ceiling: a login attempt is checked against
    # the stored digest, not against the password policy, so rejecting a short
    # password here would leak the policy and answer 422 where the truthful
    # answer is 401.
    password: str = Field(max_length=1024)


class TokenResponse(BaseModel):
    """An issued access token and how long it is good for."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.")
    expires_at: datetime

    @classmethod
    def from_view(cls, view: AccessTokenView) -> TokenResponse:
        """Build a response from an application read model."""
        return cls(
            access_token=view.access_token,
            token_type=view.token_type,
            expires_in=view.expires_in,
            expires_at=view.expires_at,
        )


class PrincipalResponse(BaseModel):
    """The authenticated caller's identity, role, and resolved permissions."""

    id: str
    email: str
    role: str
    permissions: list[str] = Field(
        description=(
            "Permissions granted by this role. Advisory for the UI only -- every "
            "one of them is enforced server-side on the request that uses it."
        )
    )

    @classmethod
    def from_view(cls, view: PrincipalView) -> PrincipalResponse:
        """Build a response from an application read model."""
        return cls(
            id=view.id,
            email=view.email,
            role=view.role,
            permissions=list(view.permissions),
        )


class ResourceAccessResponse(BaseModel):
    """The result of an object-level authorization check.

    Deliberately *not* a projection of a run, job, trace, or session: those
    records and their representations belong to tickets #17/#19/#20-22, and Ticket
    #5 does not invent them. What this body reports is the access decision itself
    -- which object was asked for and who owns it -- so the guard is observable and
    testable now. The owning tickets replace the body with the real projection and
    keep the guard in front of it.
    """

    resource_type: str
    resource_id: str
    owner_id: str
