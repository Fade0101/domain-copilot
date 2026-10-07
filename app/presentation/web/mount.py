"""Serve the web client without changing API prefixes or authentication."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_ASSETS = Path(__file__).parent / "assets"


def mount_web_ui(app: FastAPI, api_prefix: str) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            _ASSETS / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; connect-src 'self'; img-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/ui/config", include_in_schema=False)
    async def config() -> dict[str, str]:
        return {"api_prefix": api_prefix}

    app.mount("/ui/assets", StaticFiles(directory=_ASSETS), name="web-assets")
