"""FastAPI application factory.

``create_app`` builds the ASGI app, mounts routers under the configured API
prefix, and registers the domain-to-HTTP error handlers defined in
:mod:`app.presentation.api.errors`. Use cases therefore never raise
``HTTPException`` -- the framework concern stays at this edge.

Run locally with::

    uvicorn app.presentation.api.app:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.presentation.api.errors import register_exception_handlers
from app.presentation.api.routes import documents, health


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the FastAPI application."""
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    app.include_router(health.router)
    app.include_router(documents.router, prefix=settings.api_v1_str)

    register_exception_handlers(app)
    return app
