"""Authentication over real HTTP (BRD FR-8, AC-8.1, AC-8.3).

The full chain, unmocked: ``POST /api/v1/auth/token`` -> route -> ``Depends()`` ->
composition root -> use case -> bcrypt + PyJWT, then the issued token back through
``get_current_principal`` on a protected route.

The tampering tests are the ones to read first. They are the concrete form of
"never trust a role supplied by the client": each sends a request that asks to be
an admin, and each is answered as the account it actually authenticated as.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.domain.auth.value_objects import Role
from tests.integration.conftest import DEMO_EMAILS, DEMO_PASSWORD, auth, login

_TOKEN_URL = "/api/v1/auth/token"
_ME_URL = "/api/v1/auth/me"


class TestTokenIssuing:
    @pytest.mark.parametrize("role", list(Role))
    def test_each_demo_account_can_authenticate(self, client: TestClient, role: Role) -> None:
        response = client.post(
            _TOKEN_URL, json={"email": DEMO_EMAILS[role], "password": DEMO_PASSWORD}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["access_token"].count(".") == 2

    def test_email_is_matched_case_insensitively(self, client: TestClient) -> None:
        response = client.post(
            _TOKEN_URL, json={"email": "  ANALYST@Example.COM ", "password": DEMO_PASSWORD}
        )
        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("email", "password", "case"),
        [
            ("analyst@example.com", "the-wrong-password", "wrong password"),
            ("nobody@example.com", DEMO_PASSWORD, "unknown account"),
            ("not-an-email", DEMO_PASSWORD, "malformed email"),
            ("analyst@example.com", "", "empty password"),
            ("analyst@example.com", "x" * 500, "overlong password"),
        ],
    )
    def test_bad_credentials_are_401(
        self, client: TestClient, email: str, password: str, case: str
    ) -> None:
        response = client.post(_TOKEN_URL, json={"email": email, "password": password})
        assert response.status_code == 401, case
        assert response.json()["code"] == "NOT_AUTHENTICATED"

    def test_every_failure_looks_identical_to_the_client(self, client: TestClient) -> None:
        # An attacker must not be able to tell a registered address from an
        # unregistered one by comparing responses.
        wrong_password = client.post(
            _TOKEN_URL, json={"email": "analyst@example.com", "password": "nope-nope-nope"}
        )
        unknown_user = client.post(
            _TOKEN_URL, json={"email": "nobody@example.com", "password": "nope-nope-nope"}
        )
        assert wrong_password.status_code == unknown_user.status_code
        assert wrong_password.json() == unknown_user.json()

    def test_a_401_carries_the_bearer_challenge(self, client: TestClient) -> None:
        response = client.post(
            _TOKEN_URL, json={"email": "analyst@example.com", "password": "nope-nope-nope"}
        )
        assert response.headers["www-authenticate"] == "Bearer"

    def test_the_response_never_contains_a_password_hash(self, client: TestClient) -> None:
        response = client.post(
            _TOKEN_URL, json={"email": "analyst@example.com", "password": DEMO_PASSWORD}
        )
        assert "$2b$" not in response.text
        assert "hashed_password" not in response.text
        assert DEMO_PASSWORD not in response.text

    def test_the_token_payload_carries_no_credential(self, client: TestClient) -> None:
        # A JWT payload is base64, not encryption. Anyone holding the token can
        # read it, so nothing secret may be in there.
        token = login(client, Role.ANALYST)
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert "password" not in decoded
        assert not any("hash" in key.lower() for key in decoded)
        assert not any("secret" in key.lower() for key in decoded)


class TestProtectedEndpointsRejectUnauthenticated:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="no-header"),
            pytest.param({"Authorization": ""}, id="empty-header"),
            pytest.param({"Authorization": "Bearer"}, id="scheme-only"),
            pytest.param({"Authorization": "Bearer "}, id="empty-token"),
            pytest.param({"Authorization": "Bearer    "}, id="whitespace-token"),
            pytest.param({"Authorization": "Bearer not.a.jwt"}, id="malformed"),
            pytest.param({"Authorization": "Basic YWRtaW46YWRtaW4="}, id="basic-auth"),
            pytest.param({"Authorization": "Token abc123"}, id="unknown-scheme"),
            pytest.param({"Authorization": "bearer lowercase.scheme.token"}, id="bad-token"),
        ],
    )
    def test_me_requires_a_valid_bearer_token(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.get(_ME_URL, headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "NOT_AUTHENTICATED"
        # FastAPI's own HTTPBearer would answer a missing header with 403 and its
        # own body shape; auto_error=False routes it through our contract instead.
        assert response.headers["www-authenticate"] == "Bearer"

    def test_a_tampered_signature_is_rejected(self, client: TestClient) -> None:
        token = login(client, Role.ANALYST)
        response = client.get(_ME_URL, headers=auth(token[:-6] + "AAAAAA"))
        assert response.status_code == 401

    def test_a_token_with_a_swapped_payload_is_rejected(self, client: TestClient) -> None:
        # Re-sign nothing: keep the signature, swap in an admin payload.
        analyst = login(client, Role.ANALYST)
        header, payload, signature = analyst.split(".")
        raw = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        raw["role"] = "admin"
        forged = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
        response = client.get(_ME_URL, headers=auth(f"{header}.{forged}.{signature}"))
        assert response.status_code == 401

    def test_error_responses_leak_nothing(self, client: TestClient) -> None:
        response = client.get(_ME_URL, headers=auth("not.a.real.token"))
        text = response.text.lower()
        for leak in ("secret", "signature", "$2b$", "traceback", "jwt", "pyjwt", "bcrypt"):
            assert leak not in text
        assert response.json() == {"detail": "Not authenticated", "code": "NOT_AUTHENTICATED"}


class TestCurrentPrincipal:
    @pytest.mark.parametrize("role", list(Role))
    def test_me_reports_the_authenticated_account(
        self, client: TestClient, tokens: dict[Role, str], role: Role
    ) -> None:
        response = client.get(_ME_URL, headers=auth(tokens[role]))
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == DEMO_EMAILS[role]
        assert body["role"] == role.value
        assert body["id"]

    def test_me_reports_the_resolved_permission_set(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        analyst = client.get(_ME_URL, headers=auth(tokens[Role.ANALYST])).json()
        admin = client.get(_ME_URL, headers=auth(tokens[Role.ADMIN])).json()
        assert "run_workflow" in analyst["permissions"]
        assert "manage_users" not in analyst["permissions"]
        assert "manage_users" in admin["permissions"]
        assert set(analyst["permissions"]) < set(admin["permissions"])

    def test_me_never_returns_a_password_hash(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        for role in Role:
            response = client.get(_ME_URL, headers=auth(tokens[role]))
            assert "$2b$" not in response.text
            assert "hashed_password" not in response.text
            assert "password" not in response.text.lower()


class TestClientSuppliedIdentityIsIgnored:
    """Requests that ask to be someone else. None of them may succeed."""

    def test_a_role_in_the_login_body_does_not_change_the_issued_role(
        self, client: TestClient
    ) -> None:
        # The literal escalation attempt from the Ticket #5 brief.
        response = client.post(
            _TOKEN_URL,
            json={
                "email": DEMO_EMAILS[Role.ANALYST],
                "password": DEMO_PASSWORD,
                "role": "admin",
                "user_id": "admin-id",
                "owner_id": "admin-id",
            },
        )
        assert response.status_code == 200
        me = client.get(_ME_URL, headers=auth(response.json()["access_token"]))
        assert me.json()["role"] == "analyst"
        assert me.json()["email"] == DEMO_EMAILS[Role.ANALYST]

    @pytest.mark.parametrize(
        "extra_headers",
        [
            {"X-Role": "admin"},
            {"X-User-Id": "admin-id"},
            {"X-User-Role": "admin"},
            {"X-Owner-Id": "admin-id"},
            {"Role": "admin"},
            {"X-Forwarded-User": "admin@example.com"},
        ],
    )
    def test_identity_headers_are_ignored(
        self, client: TestClient, tokens: dict[Role, str], extra_headers: dict[str, str]
    ) -> None:
        response = client.get(_ME_URL, headers={**auth(tokens[Role.ANALYST]), **extra_headers})
        assert response.status_code == 200
        assert response.json()["role"] == "analyst"

    @pytest.mark.parametrize(
        "query",
        ["role=admin", "user_id=admin-id", "owner_id=admin-id", "role=admin&user_id=admin-id"],
    )
    def test_query_parameters_are_ignored(
        self, client: TestClient, tokens: dict[Role, str], query: str
    ) -> None:
        response = client.get(f"{_ME_URL}?{query}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code == 200
        assert response.json()["role"] == "analyst"

    def test_an_unauthenticated_request_cannot_assert_an_identity(self, client: TestClient) -> None:
        # No token at all, only claims about who the caller is.
        response = client.get(
            _ME_URL,
            headers={"X-Role": "admin", "X-User-Id": "admin-id"},
        )
        assert response.status_code == 401

    def test_another_accounts_email_in_the_body_does_not_borrow_its_role(
        self, client: TestClient
    ) -> None:
        # Authenticating as the analyst while naming the admin address elsewhere.
        response = client.post(
            _TOKEN_URL,
            json={
                "email": DEMO_EMAILS[Role.ANALYST],
                "password": DEMO_PASSWORD,
                "impersonate": DEMO_EMAILS[Role.ADMIN],
            },
        )
        assert response.status_code == 200
        me = client.get(_ME_URL, headers=auth(response.json()["access_token"]))
        assert me.json()["email"] == DEMO_EMAILS[Role.ANALYST]

    def test_the_admin_password_cannot_be_reused_for_another_account(
        self, client: TestClient
    ) -> None:
        # All demo accounts share a password, so this confirms the email selects
        # the account and the role follows the account, not the credential.
        token = login(client, Role.ANALYST)
        assert client.get(_ME_URL, headers=auth(token)).json()["role"] == "analyst"


class TestDemoAccounts:
    def test_exactly_the_three_documented_accounts_exist(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        roles = {
            client.get(_ME_URL, headers=auth(token)).json()["role"] for token in tokens.values()
        }
        assert roles == {"analyst", "reviewer", "admin"}

    def test_seeding_again_does_not_change_the_accounts(self, client: TestClient) -> None:
        # Idempotency at the HTTP level: the lifespan hook already seeded once on
        # startup, so a second run must leave the same ids in place.
        from app.core.container import get_container

        before = client.get(_ME_URL, headers=auth(login(client, Role.ANALYST))).json()["id"]
        import asyncio

        result = asyncio.run(get_container().seed_demo_accounts())
        after = client.get(_ME_URL, headers=auth(login(client, Role.ANALYST))).json()["id"]

        assert result is not None
        assert result.created == ()
        assert len(result.skipped) == 3
        assert before == after

    def test_an_unseeded_email_cannot_authenticate(self, client: TestClient) -> None:
        response = client.post(
            _TOKEN_URL, json={"email": "superuser@example.com", "password": DEMO_PASSWORD}
        )
        assert response.status_code == 401
