"""Domain/application error -> HTTP mapping (BRD AR-5; SDD A.5.1).

The single source of truth for how errors become HTTP responses. Use cases raise
typed domain/application errors and never touch ``HTTPException``; this edge
module translates them *by type* into a stable response body::

    {"detail": "<message>", "code": "<STABLE_CODE>"}

Responses are split by *fault*:

* **Client faults** (bad input, wrong state, missing resource) carry the error's
  own message -- it is safe and useful to the caller.
* **Server faults** (misconfiguration, unexpected exceptions) carry a fixed,
  generic message and a stable code; the real cause is logged server-side and
  **never** returned, so internal details never leak (SDD A.5.1).

Starlette resolves handlers along an exception's MRO, so a more-derived handler
(e.g. ``ConfigurationError``) wins over its base (``ApplicationError``), and the
``Exception`` catch-all only runs for anything otherwise unmapped.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.application.errors import (
    ApplicationError,
    ConfigurationError,
    ResourceNotFoundError,
)
from app.domain.shared.errors import (
    DomainError,
    InvalidStateTransitionError,
    InvariantViolationError,
)

logger = logging.getLogger("app.presentation.errors")

_CONFIG_ERROR_MESSAGE = "Application configuration error"
_INTERNAL_ERROR_MESSAGE = "Internal server error"


def _body(detail: str, code: str) -> dict[str, str]:
    return {"detail": detail, "code": code}


def register_exception_handlers(app: FastAPI) -> None:
    """Register every domain/application error handler plus the fail-safe catch-all."""

    @app.exception_handler(InvariantViolationError)
    async def _handle_invariant(_: Request, exc: InvariantViolationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_body(str(exc), "INVARIANT_VIOLATION"))

    @app.exception_handler(InvalidStateTransitionError)
    async def _handle_transition(_: Request, exc: InvalidStateTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content=_body(str(exc), "INVALID_STATE_TRANSITION"))

    @app.exception_handler(ResourceNotFoundError)
    async def _handle_not_found(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_body(str(exc), "RESOURCE_NOT_FOUND"))

    @app.exception_handler(ConfigurationError)
    async def _handle_configuration(_: Request, exc: ConfigurationError) -> JSONResponse:
        # Server fault: log the real cause, return a generic message (no leak).
        logger.error("configuration error: %s", exc, exc_info=exc)
        return JSONResponse(
            status_code=500, content=_body(_CONFIG_ERROR_MESSAGE, "CONFIGURATION_ERROR")
        )

    @app.exception_handler(DomainError)
    async def _handle_domain(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_body(str(exc), "DOMAIN_ERROR"))

    @app.exception_handler(ApplicationError)
    async def _handle_application(_: Request, exc: ApplicationError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_body(str(exc), "APPLICATION_ERROR"))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Last line of defense: never expose the exception text to the caller.
        logger.error("unhandled exception: %s", exc, exc_info=exc)
        return JSONResponse(
            status_code=500, content=_body(_INTERNAL_ERROR_MESSAGE, "INTERNAL_ERROR")
        )
