"""FastAPI application factory.

``create_app`` builds the ASGI app, mounts routers under the configured API
prefix, and registers the domain-to-HTTP error handlers defined in
:mod:`app.presentation.api.errors`. Use cases therefore never raise
``HTTPException`` -- the framework concern stays at this edge.

The lifespan hook seeds the per-role demo accounts on startup. It lives here
rather than in the container's constructor because the user repository port is
async: a synchronous ``asyncio.run`` during construction would fail under an
already-running event loop. Seeding is a no-op unless it is enabled, the
environment is not production, and a demo password is configured.

Run locally with::

    uvicorn app.presentation.api.app:create_app --factory --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.container import get_container
from app.presentation.api.correlation import CorrelationMiddleware
from app.presentation.api.errors import register_exception_handlers
from app.presentation.api.openapi import configure_openapi
from app.presentation.api.routes import (
    approvals,
    auth,
    documents,
    evaluations,
    health,
    jobs,
    knowledge,
    observability,
    resources,
    sessions,
    workflows,
)
from app.presentation.api.schemas.errors import ERROR_RESPONSES
from app.presentation.web.mount import mount_web_ui


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Seed development demo accounts on startup; release the connection pool on shutdown."""
    container = get_container()
    await container.seed_demo_accounts()
    try:
        yield
    finally:
        await container.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the FastAPI application."""
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=_lifespan,
        responses=ERROR_RESPONSES,
        description="Domain Copilot HTTP API. Bearer JWT identity and stored roles are "
        "checked server-side. See docs/API-CONTRACTS.md for ownership and SSE semantics.",
    )
    app.add_middleware(CorrelationMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router, prefix=settings.api_v1_str)
    app.include_router(documents.router, prefix=settings.api_v1_str)
    app.include_router(resources.router, prefix=settings.api_v1_str)
    app.include_router(sessions.router, prefix=settings.api_v1_str)
    app.include_router(jobs.router, prefix=settings.api_v1_str)
    app.include_router(knowledge.router, prefix=settings.api_v1_str)
    app.include_router(evaluations.router, prefix=settings.api_v1_str)
    app.include_router(approvals.router, prefix=settings.api_v1_str)
    app.include_router(observability.router, prefix=settings.api_v1_str)
    app.include_router(workflows.router, prefix=settings.api_v1_str)

    register_exception_handlers(app)
    configure_openapi(app, settings.api_v1_str)
    mount_web_ui(app, settings.api_v1_str)
    return app
