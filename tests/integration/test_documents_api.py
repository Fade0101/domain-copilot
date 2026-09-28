"""Integration test for the documents API vertical slice.

Drives the real DI chain: ``POST /api/v1/documents`` -> FastAPI route ->
``Depends()`` -> composition root -> ``RegisterDocumentUseCase`` ->
``IDocumentRepository`` -> ``InMemoryDocumentRepository``. The container is a
process singleton, so its cache is cleared per test for isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.container import get_container
from app.presentation.api.app import create_app

_VALID_HASH = "a" * 64


@pytest.fixture
def client() -> Iterator[TestClient]:
    get_container.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_container.cache_clear()


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_document_returns_201(client: TestClient) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": _VALID_HASH},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "guide.pdf"
    assert body["status"] == "REGISTERED"
    assert body["version"] == 1


def test_reregister_same_hash_returns_200_same_id(client: TestClient) -> None:
    first = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": _VALID_HASH},
    )
    second = client.post(
        "/api/v1/documents",
        json={"filename": "other.pdf", "content_hash": _VALID_HASH},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_invalid_content_hash_returns_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"filename": "guide.pdf", "content_hash": "not-a-hash"},
    )
    assert response.status_code == 422
