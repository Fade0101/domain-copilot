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
* **Authentication and authorization faults** are client faults, but they also
  carry fixed messages. Which of "no such account", "wrong password", or
  "expired token" occurred is useful to an attacker and to nobody else, so the
  401 responses are indistinguishable from one another (BRD AC-8.3).

Starlette resolves handlers along an exception's MRO, so a more-derived handler
(e.g. ``ConfigurationError``) wins over its base (``ApplicationError``), and the
``Exception`` catch-all only runs for anything otherwise unmapped.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.application.approvals.errors import ApprovalStoreError
from app.application.errors import (
    ApplicationError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    JobStoreError,
    KnowledgeUnavailableError,
    ObservabilityUnavailableError,
    ResourceNotFoundError,
    ResourceOwnershipError,
    UploadTooLargeError,
)
from app.application.sessions import HistoryStoreUnavailableError
from app.domain.shared.errors import (
    DomainError,
    InvalidStateTransitionError,
    InvariantViolationError,
)

logger = logging.getLogger("app.presentation.errors")

_CONFIG_ERROR_MESSAGE = "Application configuration error"
_INTERNAL_ERROR_MESSAGE = "Internal server error"
_UNAUTHENTICATED_MESSAGE = "Not authenticated"
_FORBIDDEN_MESSAGE = "Insufficient permissions"
_RESOURCE_FORBIDDEN_MESSAGE = "Access to this resource is forbidden"

#: RFC 9110 requires a challenge on every 401. Naming only the ``Bearer`` scheme
#: keeps clients from attempting Basic auth against this API.
_WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}


def _body(detail: str, code: str) -> dict[str, str]:
    return {"detail": detail, "code": code}


def register_exception_handlers(app: FastAPI) -> None:
    """Register every domain/application error handler plus the fail-safe catch-all."""

    @app.exception_handler(HistoryStoreUnavailableError)
    async def _handle_history(_: Request, exc: HistoryStoreUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=_body("History storage is unavailable", "HISTORY_STORE_UNAVAILABLE"),
        )

    @app.exception_handler(ObservabilityUnavailableError)
    async def _handle_observability(_: Request, exc: ObservabilityUnavailableError) -> JSONResponse:
        logger.error("trace storage unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=_body("Trace storage is unavailable", "OBSERVABILITY_UNAVAILABLE"),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors can contain passwords, prompts and raw input. Keep them private.
        return JSONResponse(
            status_code=422, content=_body("Request validation failed", "VALIDATION_ERROR")
        )

    @app.exception_handler(HTTPException)
    async def _handle_http(_: Request, exc: HTTPException) -> JSONResponse:
        from http import HTTPStatus

        try:
            detail = HTTPStatus(exc.status_code).phrase
        except ValueError:
            detail = "HTTP request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(detail, "NOT_AUTHENTICATED" if exc.status_code == 401 else "HTTP_ERROR"),
            headers={
                **(exc.headers or {}),
                **(_WWW_AUTHENTICATE if exc.status_code == 401 else {}),
            },
        )

    @app.exception_handler(ApprovalStoreError)
    async def _handle_approval_storage(_: Request, exc: ApprovalStoreError) -> JSONResponse:
        logger.error("approval storage unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=_body("Approval storage is unavailable", "APPROVAL_STORE_UNAVAILABLE"),
        )

    @app.exception_handler(KnowledgeUnavailableError)
    async def _handle_knowledge(_: Request, exc: KnowledgeUnavailableError) -> JSONResponse:
        logger.warning("Knowledge request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=_body("Knowledge service is unavailable", "KNOWLEDGE_UNAVAILABLE"),
        )

    @app.exception_handler(UploadTooLargeError)
    async def _handle_upload_size(_: Request, exc: UploadTooLargeError) -> JSONResponse:
        return JSONResponse(status_code=413, content=_body(str(exc), "UPLOAD_TOO_LARGE"))

    @app.exception_handler(InvariantViolationError)
    async def _handle_invariant(_: Request, exc: InvariantViolationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_body(str(exc), "INVARIANT_VIOLATION"))

    @app.exception_handler(InvalidStateTransitionError)
    async def _handle_transition(_: Request, exc: InvalidStateTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content=_body(str(exc), "INVALID_STATE_TRANSITION"))

    @app.exception_handler(ResourceNotFoundError)
    async def _handle_not_found(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_body(str(exc), "RESOURCE_NOT_FOUND"))

    @app.exception_handler(AuthenticationError)
    async def _handle_unauthenticated(_: Request, exc: AuthenticationError) -> JSONResponse:
        # The error type is recorded server-side; the client learns only that it
        # is not authenticated. Auth errors are constructed with fixed messages
        # that never embed a token, a hash, or a library's exception text, so
        # this log line cannot leak a credential either.
        logger.info("authentication failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=401,
            content=_body(_UNAUTHENTICATED_MESSAGE, "NOT_AUTHENTICATED"),
            headers=_WWW_AUTHENTICATE,
        )

    @app.exception_handler(ResourceOwnershipError)
    async def _handle_ownership(_: Request, exc: ResourceOwnershipError) -> JSONResponse:
        logger.info("ownership check denied access: %s", exc)
        return JSONResponse(
            status_code=403, content=_body(_RESOURCE_FORBIDDEN_MESSAGE, "RESOURCE_FORBIDDEN")
        )

    @app.exception_handler(AuthorizationError)
    async def _handle_forbidden(_: Request, exc: AuthorizationError) -> JSONResponse:
        logger.info("authorization denied: %s", exc)
        return JSONResponse(status_code=403, content=_body(_FORBIDDEN_MESSAGE, "PERMISSION_DENIED"))

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

    @app.exception_handler(JobStoreError)
    async def _handle_job_storage(_: Request, exc: JobStoreError) -> JSONResponse:
        logger.error("durable job storage unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=_body("Job storage is unavailable", "JOB_STORE_UNAVAILABLE"),
        )

    @app.exception_handler(ApplicationError)
    async def _handle_application(_: Request, exc: ApplicationError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_body(str(exc), "APPLICATION_ERROR"))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Last line of defense: never expose the exception text to the caller.
        logger.error("unhandled exception: %s", exc, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_body(_INTERNAL_ERROR_MESSAGE, "INTERNAL_ERROR"),
            headers={"X-Correlation-ID": getattr(request.state, "correlation_id", "")},
        )
