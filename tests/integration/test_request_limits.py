"""Request-body limits enforced at the HTTP edge (Ticket #26, OWASP API4:2023).

Starlette buffers a request body before a handler sees it, so a pydantic
``max_length`` cannot bound memory: by the time the constraint runs, the bytes are
already in the process. These tests target the middleware that runs *before* that,
and they drive the real ``create_app`` rather than a hand-built app, so what is
proven is the wiring and the ordering (outermost, ahead of routing and dependency
resolution) rather than the class in isolation.

Two shapes of oversized request are covered, because they fail differently:

* a declared ``Content-Length`` over the limit -- refused outright with 413
  without reading the body at all;
* no ``Content-Length`` (chunked) -- nothing can be trusted up front, so the
  stream is counted and truncated, and the app rejects the truncated request.

The exemption for document upload is tested from both sides: the ingest route must
not answer 413 from this middleware (it owns a larger, streamed budget) while other
POSTs must.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from app.core.config import ApiSettings, Settings
from app.presentation.api.app import create_app
from app.presentation.api.request_limits import (
    REQUEST_TOO_LARGE,
    RequestSizeLimitMiddleware,
)

#: Small enough to build bodies in memory cheaply, large enough to be obviously
#: distinct from anything the app would legitimately accept.
_TEST_LIMIT = 4096

_LOGIN_ROUTE = "/api/v1/auth/token"
_INGEST_ROUTE = "/api/v1/documents/ingest?filename=note.md"


def _settings(max_request_bytes: int) -> Settings:
    """Settings for a test, independent of any developer ``.env`` on the machine."""
    return Settings(_env_file=None, api=ApiSettings(max_request_bytes=max_request_bytes))


@pytest.fixture
def limited_app() -> FastAPI:
    return create_app(_settings(_TEST_LIMIT))


@pytest.fixture
def limited_client(limited_app: FastAPI) -> Iterator[TestClient]:
    # raise_server_exceptions=False so an oversized request that reaches a handler
    # and blows up there surfaces as a 500 we can assert on, not a raised exception.
    with TestClient(limited_app, raise_server_exceptions=False) as client:
        yield client


def _body_of(byte_count: int) -> bytes:
    """A syntactically valid JSON object of exactly ``byte_count`` bytes.

    Padding rather than random bytes, so a rejection can only be the size limit and
    never a parse error masking it.
    """
    prefix = b'{"filler":"'
    suffix = b'"}'
    pad = max(byte_count - len(prefix) - len(suffix), 0)
    return prefix + (b"a" * pad) + suffix


def _post(client: TestClient, content: object, **kwargs: object) -> object:
    return client.post(
        _LOGIN_ROUTE,
        content=content,  # type: ignore[arg-type]
        headers={"content-type": "application/json"},
        **kwargs,  # type: ignore[arg-type]
    )


class TestDeclaredContentLength:
    def test_a_body_over_the_limit_is_refused_with_413(self, limited_client: TestClient) -> None:
        response = _post(limited_client, _body_of(_TEST_LIMIT * 2))
        assert response.status_code == REQUEST_TOO_LARGE, response.text
        assert response.json()["code"] == "REQUEST_TOO_LARGE"

    def test_the_refusal_states_the_limit_but_not_the_received_size(
        self, limited_client: TestClient
    ) -> None:
        # The configured limit is actionable; echoing how far over the caller went
        # is an oracle for walking the boundary down one byte at a time.
        response = _post(limited_client, _body_of(_TEST_LIMIT + 1))
        assert response.headers["x-max-request-bytes"] == str(_TEST_LIMIT)
        assert str(_TEST_LIMIT + 1) not in response.text

    def test_a_body_within_the_limit_reaches_the_handler(self, limited_client: TestClient) -> None:
        # 401 rather than 413: the limit admitted the request and authentication
        # rejected it, which is what proves the middleware forwarded rather than
        # answered.
        response = _post(
            limited_client, json.dumps({"email": "nobody@example.com", "password": "x" * 16})
        )
        assert response.status_code == 401, response.text

    def test_a_body_of_exactly_the_limit_is_admitted(self, limited_client: TestClient) -> None:
        # The bound is inclusive. An off-by-one here would reject the largest body
        # the configuration says is permitted.
        response = _post(limited_client, _body_of(_TEST_LIMIT))
        assert response.status_code != REQUEST_TOO_LARGE, response.text

    def test_a_non_numeric_content_length_is_refused(self, limited_client: TestClient) -> None:
        # Nothing downstream can bound the read if the declaration is unusable, so
        # guessing is not an option.
        response = limited_client.post(
            _LOGIN_ROUTE,
            content=b"{}",
            headers={"content-type": "application/json", "content-length": "not-a-number"},
        )
        assert response.status_code == REQUEST_TOO_LARGE, response.text


class TestStreamedBodyWithoutContentLength:
    def test_a_chunked_body_over_the_limit_is_not_accepted(
        self, limited_client: TestClient
    ) -> None:
        # httpx sends a generator body with Transfer-Encoding: chunked and no
        # Content-Length, so this is the real "declaration omitted" path rather
        # than a simulation of it.
        def chunks() -> Iterator[bytes]:
            for _ in range(8):
                yield b"a" * _TEST_LIMIT

        response = _post(limited_client, chunks())

        # The app is already parsing when the truncation lands, so it answers its
        # own 4xx rather than this middleware answering 413. Either is fine; what
        # matters is that the oversized body is not accepted.
        assert response.status_code >= 400, response.text
        assert response.status_code != REQUEST_TOO_LARGE or response.json()["code"] == (
            "REQUEST_TOO_LARGE"
        )

    def test_a_chunked_body_within_the_limit_is_admitted(self, limited_client: TestClient) -> None:
        # The mirror of the case above: counting must not reject a legitimate
        # streamed body, or the no-Content-Length path would be a blanket refusal.
        payload = json.dumps({"email": "nobody@example.com", "password": "x" * 16}).encode()

        def chunks() -> Iterator[bytes]:
            yield payload[:10]
            yield payload[10:]

        response = _post(limited_client, chunks())
        assert response.status_code == 401, response.text


class TestMiddlewareContract:
    """ASGI-level checks for boundaries the HTTP surface cannot express."""

    @staticmethod
    def _scope(method: str, path: str) -> Scope:
        return {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 12345),
            "root_path": "",
        }

    def test_body_less_methods_are_never_refused(self) -> None:
        seen: list[str] = []
        sent: list[Message] = []

        async def app(scope: Scope, receive: object, send: object) -> None:
            seen.append(str(scope["method"]))

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        # A one-byte limit: if the method check were missing or wrong, every
        # request below would be refused.
        middleware = RequestSizeLimitMiddleware(app, max_bytes=1)  # type: ignore[arg-type]

        for method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            asyncio.run(limited_call(middleware, self._scope(method, "/api/v1/auth/me")))

        assert seen == ["GET", "HEAD", "OPTIONS", "DELETE"]
        assert sent == []

    def test_a_limit_of_zero_or_less_is_rejected_at_construction(self) -> None:
        async def app(scope: Scope, receive: object, send: object) -> None:
            return None

        for bad in (0, -1):
            with pytest.raises(ValueError):
                RequestSizeLimitMiddleware(app, max_bytes=bad)  # type: ignore[arg-type]


async def limited_call(middleware: RequestSizeLimitMiddleware, scope: Scope) -> None:
    """Drive the middleware with a no-op receive/send pair."""

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_: Message) -> None:
        return None

    await middleware(scope, receive, send)


class TestExemptions:
    def test_the_ingest_route_is_not_capped_by_this_limit(self, limited_client: TestClient) -> None:
        # Ingestion streams against ingestion.max_upload_bytes, which is larger than
        # api.max_request_bytes. Capping it here would silently shrink the documented
        # upload budget, so this middleware must stand aside and let the route (or
        # authentication) answer.
        response = limited_client.post(
            _INGEST_ROUTE,
            content=b"# " + (b"a" * (_TEST_LIMIT * 2)),
            headers={"content-type": "text/markdown"},
        )
        assert response.status_code != REQUEST_TOO_LARGE, response.text


class TestConfiguration:
    def test_the_default_limit_admits_a_normal_json_body(self) -> None:
        # An edited clinical note plus a rejection reason is tens of kilobytes. A
        # default below that would be a functional regression, not a control.
        assert ApiSettings().max_request_bytes >= 262_144

    def test_the_default_limit_sits_below_the_upload_budget(self) -> None:
        # Uploads stream against their own larger budget, so the JSON limit must not
        # be the reason a legitimate document upload fails.
        settings = Settings(_env_file=None)
        assert settings.api.max_request_bytes < settings.ingestion.max_upload_bytes

    def test_the_limit_is_configurable_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API__MAX_REQUEST_BYTES", "2048")
        assert Settings(_env_file=None).api.max_request_bytes == 2048

    def test_an_out_of_range_limit_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API__MAX_REQUEST_BYTES", "0")
        with pytest.raises(ValueError):
            Settings(_env_file=None)
