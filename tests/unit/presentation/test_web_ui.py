"""The web client ships with the API and works with a non-default API prefix."""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.presentation.api.app import create_app


def test_web_assets_config_and_openapi_are_served_without_auth() -> None:
    app = create_app(Settings(api_v1_str="/custom/v1"))
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Domain Copilot" in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/ui/config").json() == {"api_prefix": "/custom/v1"}
        for file in ("styles.css", "app.js", "api.js", "stream.js", "model.js", "render.js"):
            assert client.get(f"/ui/assets/{file}").status_code == 200
        spec = client.get("/openapi.json").json()
        assert "/custom/v1/ask" in spec["paths"]
        assert "/" not in spec["paths"] and "/ui/config" not in spec["paths"]


def test_web_does_not_interpret_model_html_or_store_bearer_tokens() -> None:
    client = TestClient(create_app())
    for asset in ("app.js", "render.js"):
        source = client.get(f"/ui/assets/{asset}").text
        assert ".innerHTML" not in source
        assert "insertAdjacentHTML" not in source
        assert "localStorage" not in source and "sessionStorage" not in source
    assert client.get("/ui/assets/%2e%2e/mount.py").status_code == 404
