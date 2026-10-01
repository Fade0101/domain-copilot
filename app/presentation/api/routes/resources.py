"""Object-ownership-protected resource routes (BRD AC-8.4, SEC-1a).

The remaining access projections for ``GET /runs/{run_id}``,
``/traces/{trace_id}``, ``/sessions/{session_id}``. Jobs have a full projection
in ``routes/jobs.py`` using the same authorization service. Each answers the question
"may *this* caller see *this* object?" by consulting persisted ownership.

What makes these resistant to IDOR/BOLA is what they do *not* rely on:

* not the token alone -- a valid token for user A does not open user B's run;
* not the id being unguessable -- a random or substituted UUID is looked up and
  found to have no owner, which is a refusal, not a pass;
* not client-side filtering -- the decision is made here, server-side, before any
  body is built;
* not a client-supplied ``owner_id`` or ``user_id``. None of these routes declares
  such a parameter, so an extra query string or header is inert.

Each route is four lines and delegates the decision to
:class:`AuthorizationService`. The resource type is the only thing that varies, and
it is passed as a typed :class:`ResourceType`; no role strings appear here.

The response body reports the access decision rather than a resource projection --
see :class:`ResourceAccessResponse` for why that is not a placeholder.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.domain.auth.value_objects import ResourceType
from app.presentation.api.schemas.auth import ResourceAccessResponse
from app.presentation.api.security import get_authorization_service, get_current_principal

router = APIRouter(tags=["resources"])

_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing, malformed, or expired token"},
    403: {"description": "Authenticated, but not permitted to access this object"},
    404: {"description": "No such object"},
}


async def _authorized_access(
    resource_type: ResourceType,
    resource_id: str,
    principal: Principal,
    authorization: AuthorizationService,
) -> ResourceAccessResponse:
    """Enforce object-level access and describe the decision.

    Raises before returning if the principal may not read this object, so there is
    no path from a refused check to a response body.
    """
    owner_id = await authorization.require_resource_access(
        principal=principal, resource_type=resource_type, resource_id=resource_id
    )
    return ResourceAccessResponse(
        resource_type=resource_type.value,
        resource_id=resource_id,
        owner_id=owner_id.value,
    )


@router.get(
    "/runs/{run_id}",
    response_model=ResourceAccessResponse,
    summary="Read a workflow run the caller is allowed to see",
    responses=_RESPONSES,
)
async def read_run(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ResourceAccessResponse:
    return await _authorized_access(ResourceType.RUN, run_id, principal, authorization)


@router.get(
    "/traces/{trace_id}",
    response_model=ResourceAccessResponse,
    summary="Read a trace the caller is allowed to see",
    responses=_RESPONSES,
)
async def read_trace(
    trace_id: str,
    principal: Principal = Depends(get_current_principal),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ResourceAccessResponse:
    return await _authorized_access(ResourceType.TRACE, trace_id, principal, authorization)


@router.get(
    "/sessions/{session_id}",
    response_model=ResourceAccessResponse,
    summary="Read a session the caller is allowed to see",
    responses=_RESPONSES,
)
async def read_session(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ResourceAccessResponse:
    return await _authorized_access(ResourceType.SESSION, session_id, principal, authorization)
