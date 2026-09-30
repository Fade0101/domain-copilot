"""Integration test for the documents API vertical slice.

Drives the real DI chain: ``POST /api/v1/documents`` -> FastAPI route ->
``Depends()`` -> composition root -> ``RegisterDocumentUseCase`` ->
``IDocumentRepository`` -> ``InMemoryDocumentRepository``.

Ticket #5 put document ingestion behind the admin-only ``INGEST_DOCUMENTS``
permission, so these tests now authenticate first. The fixtures come from
``tests/integration/conftest.py``, which boots the app with demo accounts seeded;
the authorization behaviour itself is asserted in ``test_rbac_api.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.auth.value_objects import Role
from tests.integration.conftest import auth

_VALID_HASH = "a" * 64
_OTHER_HASH = "b" * 64


@pytest.fixture
def admin_headers(tokens: dict[Role, str]) -> dict[str, str]:
    """Bearer header for the seeded admin account."""
    return auth(tokens[Role.ADMIN])


def test_health_endpoint(client: TestClient) -> None:
    # Unauthenticated on purpose: a health probe has no credentials to present.
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_document_returns_201(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": _VALID_HASH},
        headers=admin_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "guide.pdf"
    assert body["status"] == "REGISTERED"
    assert body["version"] == 1


def test_reregister_same_hash_returns_200_same_id(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    first = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": _OTHER_HASH},
        headers=admin_headers,
    )
    second = client.post(
        "/api/v1/documents",
        json={"filename": "other.pdf", "content_hash": _OTHER_HASH},
        headers=admin_headers,
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_invalid_content_hash_returns_422(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": "not-a-hash"},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_registering_a_document_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": "c" * 64},
    )
    assert response.status_code == 401


def test_the_response_never_exposes_a_credential(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": "d" * 64},
        headers=admin_headers,
    )
    assert "$2b$" not in response.text
    assert "hashed_password" not in response.text
