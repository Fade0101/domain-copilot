"""Integration test for domain->HTTP error mapping (BRD AR-5; SDD A.5.1).

Mounts a throwaway app whose routes deliberately raise each mapped error, then
asserts the status code and ``{detail, code}`` body produced by the real
``register_exception_handlers``. Crucially it proves that *server-fault*
responses (configuration + unexpected) never leak internal detail to the caller.

``raise_server_exceptions=False`` stops the TestClient from re-raising the
deliberately-thrown exception, so the 500 response can be inspected.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.errors import (
    ApplicationError,
    JobNotFoundError,
    PromptValidationError,
    ResourceNotFoundError,
)
from app.domain.shared.errors import (
    DomainError,
    InvalidStateTransitionError,
    InvariantViolationError,
)
from app.presentation.api.errors import register_exception_handlers

_LEAK_MARKER = "secret-internal-detail-should-not-leak"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/invariant")
    async def _invariant() -> None:
        raise InvariantViolationError("bad filename")

    @app.get("/transition")
    async def _transition() -> None:
        raise InvalidStateTransitionError("cannot skip states")

    @app.get("/not-found")
    async def _not_found() -> None:
        raise JobNotFoundError("job 42 not found")

    @app.get("/domain")
    async def _domain() -> None:
        raise DomainError("generic domain failure")

    @app.get("/application")
    async def _application() -> None:
        raise ApplicationError("generic application failure")

    @app.get("/config")
    async def _config() -> None:
        raise PromptValidationError(_LEAK_MARKER)

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError(_LEAK_MARKER)

    register_exception_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def test_invariant_maps_to_422(client: TestClient) -> None:
    response = client.get("/invariant")
    assert response.status_code == 422
    assert response.json() == {"detail": "bad filename", "code": "INVARIANT_VIOLATION"}


def test_transition_maps_to_409(client: TestClient) -> None:
    response = client.get("/transition")
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_STATE_TRANSITION"


def test_resource_not_found_maps_to_404(client: TestClient) -> None:
    response = client.get("/not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "RESOURCE_NOT_FOUND"
    assert body["detail"] == "job 42 not found"


def test_domain_maps_to_400(client: TestClient) -> None:
    response = client.get("/domain")
    assert response.status_code == 400
    assert response.json()["code"] == "DOMAIN_ERROR"


def test_application_maps_to_400(client: TestClient) -> None:
    response = client.get("/application")
    assert response.status_code == 400
    assert response.json()["code"] == "APPLICATION_ERROR"


def test_configuration_error_is_500_and_does_not_leak(client: TestClient) -> None:
    response = client.get("/config")
    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "Application configuration error", "code": "CONFIGURATION_ERROR"}
    assert _LEAK_MARKER not in response.text


def test_unexpected_error_is_500_and_does_not_leak(client: TestClient) -> None:
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "Internal server error", "code": "INTERNAL_ERROR"}
    assert _LEAK_MARKER not in response.text


def test_subclass_of_resource_not_found_uses_base_handler() -> None:
    # JobNotFoundError has no dedicated handler; it must resolve via its base.
    assert issubclass(JobNotFoundError, ResourceNotFoundError)
