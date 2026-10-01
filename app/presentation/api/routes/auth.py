"""Authentication API routes (BRD FR-8).

``POST /api/v1/auth/token`` exchanges credentials for a JWT; ``GET /api/v1/auth/me``
reports who the bearer of a token is, as the server sees them. The second is the
observable proof of the first: whatever a client claims about itself, ``/auth/me``
answers from the stored user record.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.application.auth.commands import AuthenticateUserCommand
from app.application.auth.context import Principal
from app.application.auth.dto import PrincipalView
from app.application.auth.use_cases import AuthenticateUserUseCase
from app.presentation.api.dependencies import get_authenticate_user_use_case
from app.presentation.api.schemas.auth import (
    PrincipalResponse,
    TokenRequest,
    TokenResponse,
)
from app.presentation.api.security import get_current_principal

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/token",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for an access token",
    responses={401: {"description": "Invalid email or password"}},
)
async def issue_token(
    payload: TokenRequest,
    use_case: AuthenticateUserUseCase = Depends(get_authenticate_user_use_case),
) -> TokenResponse:
    """Authenticate and issue a token.

    Unauthenticated by design -- this is where a caller becomes authenticated. An
    unknown email and a wrong password produce the same 401, having done the same
    amount of work.
    """
    result = await use_case.execute(
        AuthenticateUserCommand(email=payload.email, password=payload.password)
    )
    return TokenResponse.from_view(result.token)


@router.get(
    "/me",
    response_model=PrincipalResponse,
    summary="Describe the authenticated caller",
    responses={401: {"description": "Missing, malformed, or expired token"}},
)
async def read_current_principal(
    principal: Principal = Depends(get_current_principal),
) -> PrincipalResponse:
    """Return the caller's server-side identity, role, and permissions.

    The role reported here is read from the user record, not from the token's
    ``role`` claim, so it reflects the caller's current privileges.
    """
    return PrincipalResponse.from_view(PrincipalView.from_principal(principal))
