import httpx
import pytest

from app.infrastructure.observability.health import ChatProbe


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"data": [{"id": "openai/gpt-oss-20b", "active": True}]}, True),
        ({"data": [{"id": "openai/gpt-oss-20b"}]}, True),
        ({"data": [{"id": "openai/gpt-oss-20b", "active": False}]}, False),
        ({"data": [{"id": "different-model", "active": True}]}, False),
        ({"data": []}, False),
        ({"data": [None]}, False),
        ({"data": {}}, False),
        ([], False),
    ],
)
async def test_groq_readiness_checks_configured_model_in_list(
    monkeypatch: pytest.MonkeyPatch, body: object, expected: bool
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.groq.com/openai/v1/models"
        assert request.headers["Authorization"] == "Bearer synthetic-readiness-key"
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient

    def client(*, timeout: float) -> httpx.AsyncClient:
        return original(timeout=timeout, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    probe = ChatProbe("groq", "openai/gpt-oss-20b", api_key="synthetic-readiness-key")
    assert await probe.check() is expected


async def test_groq_readiness_does_not_accept_missing_key() -> None:
    assert not await ChatProbe("groq", "openai/gpt-oss-20b").check()
