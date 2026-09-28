"""FastAPI application factory + domain-to-HTTP error mapping.

``create_app`` builds the ASGI app, mounts routers under the configured API
prefix, and registers exception handlers that translate the domain/application
error taxonomies to HTTP status codes *by type*. Use cases therefore never raise
``HTTPException`` -- the framework concern stays at this edge.

Run locally with::

    uvicorn app.presentation.api.app:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.application.errors import ApplicationError, ResourceNotFoundError
from app.core.config import Settings, get_settings
from app.domain.shared.errors import (
    DomainError,
    InvalidStateTransitionError,
    InvariantViolationError,
)
from app.presentation.api.routes import documents, health


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the FastAPI application."""
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    app.include_router(health.router)
    app.include_router(documents.router, prefix=settings.api_v1_str)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain/application errors to HTTP responses by exception type.

    Starlette resolves handlers along an exception's MRO, so the specific
    handlers below win over the ``DomainError``/``ApplicationError`` fallbacks.
    """

    @app.exception_handler(InvariantViolationError)
    async def _handle_invariant(_: Request, exc: InvariantViolationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(InvalidStateTransitionError)
    async def _handle_transition(_: Request, exc: InvalidStateTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ResourceNotFoundError)
    async def _handle_not_found(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def _handle_domain(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ApplicationError)
    async def _handle_application(_: Request, exc: ApplicationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
