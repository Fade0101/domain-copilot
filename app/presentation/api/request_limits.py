"""Inbound request-body limit (OWASP API4:2023 Unrestricted Resource Consumption).

Starlette reads a request body into memory before routing to a handler, so a
field-level ``max_length`` in a pydantic schema is evaluated *after* the memory is
already committed. An unbounded ``POST`` therefore costs the server memory even
though the request is ultimately rejected. This middleware is the outer bound that
runs first.

Written as raw ASGI rather than ``BaseHTTPMiddleware`` on purpose:
``BaseHTTPMiddleware`` wraps the receive channel and streams the body itself,
which is exactly the allocation this control exists to prevent. Here the
``Content-Length`` header is checked before the body is read at all, so an
oversized declared body is refused without touching it.

Two cases, both enforced:

* **Declared length over the limit** -- refused immediately with 413, no read.
* **No declared length** (e.g. ``Transfer-Encoding: chunked``) -- the header
  cannot be trusted, so the streamed body is counted as it arrives and the
  remaining chunks are dropped once the running total exceeds the limit. A client
  cannot escape the bound by omitting ``Content-Length``. The request is not
  answered 413 in this case: the app is mid-parse, so it finishes its own
  handling and answers its own 4xx. Either way the bytes are bounded.

Streamed document uploads are exempt only where they already enforce their own
larger budget: the ingestion route bounds bytes while streaming them
(``ingestion.max_upload_bytes``) and rejects overflow before creating a source or
job. Exempting it here avoids a second, smaller ceiling silently winning.

Response bodies are never buffered by this middleware -- it only inspects the
request and forwards everything else untouched.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Status for a body over the configured limit (RFC 9110 15.5.14).
REQUEST_TOO_LARGE = 413

#: Routes that enforce their own streamed byte budget and must not be capped here.
#: Normalized and exact-matched against request path to prevent prefix/suffix spoofing.
_SELF_BOUNDED_PATHS = frozenset({"/documents/ingest", "/api/v1/documents/ingest"})

_METHODS_WITH_BODIES = frozenset({"POST", "PUT", "PATCH"})

#: The refusal body. Deliberately static: it does not echo the received size,
#: which would be an oracle for probing the limit, and a legitimate client
#: already knows what it sent. The configured limit itself is safe to state.
_REFUSAL_BODY = b'{"detail":"Request body exceeds the configured limit","code":"REQUEST_TOO_LARGE"}'


class RequestSizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` before they are buffered."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self._app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is None:
            # No usable Content-Length, so the declared size cannot bound the
            # read. Count the stream instead.
            await self._app(scope, self._counting_receive(receive), send)
            return

        try:
            length = int(declared)
        except ValueError:
            # A non-numeric Content-Length is malformed. Refuse rather than
            # guess, since whatever body follows would then be unbounded.
            await self._refuse(send)
            return

        if length > self._max_bytes:
            await self._refuse(send)
            return

        # Declared length is within the limit, so the body is bounded as read.
        await self._app(scope, receive, send)

    def _applies(self, scope: Scope) -> bool:
        """Whether this request is subject to the limit at all."""
        if scope.get("type") != "http" or scope.get("method") not in _METHODS_WITH_BODIES:
            return False
        path = scope.get("path", "").rstrip("/")
        return path not in _SELF_BOUNDED_PATHS

    def _counting_receive(self, receive: Receive) -> Receive:
        """Wrap ``receive`` so the streamed body cannot exceed the limit.

        Once the running total passes the limit, every remaining chunk is
        replaced with an empty body. The application therefore sees a truncated
        request and fails its own validation, while the bytes this process holds
        stay bounded by the limit plus one chunk.
        """
        total = 0
        max_bytes = self._max_bytes
        exceeded = False

        async def counting_receive() -> Message:
            nonlocal total, exceeded
            message = await receive()
            if message["type"] != "http.request" or exceeded:
                return message
            total += len(message.get("body", b""))
            if total > max_bytes:
                exceeded = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return message

        return counting_receive

    async def _refuse(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": REQUEST_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REFUSAL_BODY)).encode()),
                    (b"x-max-request-bytes", str(self._max_bytes).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REFUSAL_BODY})
