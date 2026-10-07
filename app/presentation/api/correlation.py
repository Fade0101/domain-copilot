"""ASGI correlation propagation, including streaming responses and error responses."""

from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.application.observability.context import correlation_scope


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("X-Correlation-ID", "")
        try:
            correlation_id = str(UUID(supplied))
        except ValueError:
            correlation_id = str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Correlation-ID"] = correlation_id
            await send(message)

        with correlation_scope(correlation_id):
            await self.app(scope, receive, send_with_correlation)
