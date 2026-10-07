"""Request-scoped authentication and authorization dependencies (BRD AC-8.3).

The bridge between an HTTP request and a typed :class:`Principal`. A route states
what it needs -- authentication, a permission, or access to a specific object --
and gets it through ``Depends()``; it never inspects a header or compares a role
string itself.

Where identity comes from, and where it does not
------------------------------------------------
Identity is derived from the ``Authorization: Bearer`` token and nothing else.
There is no code path here that reads a role, a user id, or an owner id out of a
request body, a query string, or a custom header. A request carrying
``{"user_id": "admin-id", "role": "admin"}`` -- or ``X-Role: admin`` -- is
authenticated exactly as its token says and no further, because those values are
never looked at.

``HTTPBearer(auto_error=False)`` is deliberate. Left to raise on its own, FastAPI's
bearer scheme answers a *missing* ``Authorization`` header with 403 and a body
shape of its own, which is both the wrong status and outside our error contract.
Returning ``None`` instead lets the missing-credentials case travel through the
same typed error taxonomy as every other failure, producing a 401 with
``WWW-Authenticate: Bearer``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import ExecutionContext, Principal
from app.application.auth.use_cases import ResolvePrincipalUseCase
from app.application.errors import MissingCredentialsError
from app.core.container import get_container
from app.domain.auth.value_objects import Permission
from app.presentation.api.dependencies import get_resolve_principal_use_case

#: ``auto_error=False``: see the module docstring. Also surfaces the scheme in
#: the OpenAPI document, so /docs offers an Authorize button.
bearer_scheme = HTTPBearer(
    auto_error=False, scheme_name="BearerAuth", description="JWT access token"
)


def get_authorization_service() -> AuthorizationService:
    """Provide the single authorization service built by the composition root."""
    return get_container().authorization_service


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    use_case: ResolvePrincipalUseCase = Depends(get_resolve_principal_use_case),
) -> Principal:
    """Resolve the bearer token into the authenticated principal, or raise 401.

    Raises :class:`MissingCredentialsError` when no usable ``Bearer`` credential
    was presented -- which includes a request with no ``Authorization`` header at
    all, and one using a different scheme such as ``Basic``. Invalid, expired, and
    orphaned tokens raise from the use case. All of them answer 401.
    """
    if credentials is None or not credentials.credentials.strip():
        raise MissingCredentialsError("no bearer credentials presented")
    return await use_case.execute(credentials.credentials)


def require_permission(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that admits only principals holding ``permission``.

    Used as ``principal: Principal = Depends(require_permission(Permission.X))``, so
    a route declares the capability it needs and the check cannot be forgotten in
    the body. Returns the principal, so a route needs only the one dependency.
    """

    async def _dependency(
        principal: Principal = Depends(get_current_principal),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ) -> Principal:
        authorization.require_permission(principal, permission)
        return principal

    return _dependency


def get_execution_context(
    principal: Principal = Depends(get_current_principal),
) -> ExecutionContext:
    """Provide the serializable identity envelope for work that outlives the request.

    The hand-off point for tickets #17/#19/#20-23: a route that enqueues a job or
    starts a workflow passes this along so the work stays attributed to the user
    who asked for it.
    """
    return ExecutionContext.from_principal(principal)
