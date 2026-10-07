"""Object-level authorization over real HTTP (BRD AC-8.4, SEC-1a).

These are the IDOR/BOLA tests. Each one takes a validly authenticated caller --
nothing here is about bad tokens -- and tries to reach an object that belongs to
somebody else, by substituting an id, guessing one, or asserting ownership in the
request.

The rule being tested is that the decision comes from persisted ownership. Not
from the token, not from the id being hard to guess, and not from the client's own
account of who owns what.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.domain.auth.value_objects import ResourceType, Role, UserId
from tests.integration.conftest import auth, own

_PATHS = {
    "runs": ResourceType.RUN,
    "jobs": ResourceType.JOB,
    "traces": ResourceType.TRACE,
    "sessions": ResourceType.SESSION,
}
_PATH_IDS = sorted(_PATHS)


class TestOwnerAccess:
    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_the_owner_can_read_its_own_object(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId], path: str
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code == 200
        body = response.json()
        assert body["resource_id"] == resource_id
        assert body["owner_id"] == user_ids[Role.ANALYST].value

    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_ownership_is_per_object_not_per_type(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId], path: str
    ) -> None:
        # Owning one run does not grant access to another user's run.
        mine = str(uuid.uuid4())
        theirs = str(uuid.uuid4())
        own(_PATHS[path], mine, user_ids[Role.ANALYST])
        own(_PATHS[path], theirs, user_ids[Role.REVIEWER])

        assert (
            client.get(f"/api/v1/{path}/{mine}", headers=auth(tokens[Role.ANALYST])).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/{path}/{theirs}", headers=auth(tokens[Role.ANALYST])).status_code
            == 403
        )


class TestCrossUserAccessIsRefused:
    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_an_analyst_cannot_read_another_analysts_object(
        self, client: TestClient, tokens: dict[Role, str], path: str
    ) -> None:
        # A second analyst-owned object. The owner is a user id that is not the
        # caller's, which is the only thing that matters to the check.
        other_analyst = UserId(str(uuid.uuid4()))
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, other_analyst)
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code == 403
        assert response.json()["code"] == "RESOURCE_FORBIDDEN"

    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_the_refusal_body_describes_nothing_about_the_object(
        self, client: TestClient, tokens: dict[Role, str], path: str
    ) -> None:
        owner = UserId(str(uuid.uuid4()))
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, owner)
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.json() == {
            "detail": "Access to this resource is forbidden",
            "code": "RESOURCE_FORBIDDEN",
        }
        # In particular, not who does own it.
        assert owner.value not in response.text


class TestIdSubstitution:
    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_swapping_the_uuid_does_not_grant_access(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId], path: str
    ) -> None:
        # The classic IDOR probe: read one of your own objects, then change the id
        # in the URL to someone else's.
        mine = str(uuid.uuid4())
        theirs = str(uuid.uuid4())
        own(_PATHS[path], mine, user_ids[Role.ANALYST])
        own(_PATHS[path], theirs, user_ids[Role.REVIEWER])

        first = client.get(f"/api/v1/{path}/{mine}", headers=auth(tokens[Role.ANALYST]))
        assert first.status_code == 200
        swapped = client.get(f"/api/v1/{path}/{theirs}", headers=auth(tokens[Role.ANALYST]))
        assert swapped.status_code == 403

    @pytest.mark.parametrize("path", _PATH_IDS)
    @pytest.mark.parametrize("role", list(Role))
    def test_a_random_id_is_never_an_authorization_bypass(
        self, client: TestClient, tokens: dict[Role, str], path: str, role: Role
    ) -> None:
        # Nothing is registered under this id. No role may get a 2xx.
        response = client.get(f"/api/v1/{path}/{uuid.uuid4()}", headers=auth(tokens[role]))
        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"

    @pytest.mark.parametrize("path", _PATH_IDS)
    @pytest.mark.parametrize(
        "hostile_id",
        [
            "00000000-0000-0000-0000-000000000000",
            "not-a-uuid",
            "1",
            "-1",
            "null",
            "undefined",
            "%20",
            "1'%20OR%20'1'='1",
            "..%2f..%2fusers",
            "*",
        ],
    )
    def test_a_hostile_id_is_refused_not_an_error(
        self, client: TestClient, tokens: dict[Role, str], path: str, hostile_id: str
    ) -> None:
        # Never a 5xx: a malformed id must reach the same refusal as any other
        # unknown id, and must not raise out of the authorization check.
        response = client.get(f"/api/v1/{path}/{hostile_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code in {403, 404}, response.text

    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_the_owners_own_id_is_not_a_valid_resource_id(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId], path: str
    ) -> None:
        # Substituting your user id for a resource id must not confuse the check
        # into matching owner == resource.
        response = client.get(
            f"/api/v1/{path}/{user_ids[Role.ANALYST].value}",
            headers=auth(tokens[Role.ANALYST]),
        )
        assert response.status_code == 404


class TestClientAssertedOwnershipIsIgnored:
    @pytest.mark.parametrize("path", _PATH_IDS)
    @pytest.mark.parametrize(
        "query",
        ["owner_id", "user_id", "role"],
    )
    def test_a_query_parameter_cannot_claim_ownership(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        user_ids: dict[Role, UserId],
        path: str,
        query: str,
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, user_ids[Role.REVIEWER])
        # Ask to be the owner, or to be an admin, in the query string.
        value = "admin" if query == "role" else user_ids[Role.ANALYST].value
        response = client.get(
            f"/api/v1/{path}/{resource_id}?{query}={value}",
            headers=auth(tokens[Role.ANALYST]),
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_headers_cannot_claim_ownership(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId], path: str
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, user_ids[Role.REVIEWER])
        response = client.get(
            f"/api/v1/{path}/{resource_id}",
            headers={
                **auth(tokens[Role.ANALYST]),
                "X-Role": "admin",
                "X-User-Id": user_ids[Role.REVIEWER].value,
                "X-Owner-Id": user_ids[Role.REVIEWER].value,
            },
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_an_unauthenticated_request_is_refused_before_ownership_matters(
        self, client: TestClient, user_ids: dict[Role, UserId], path: str
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(_PATHS[path], resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


class TestUnownedObjects:
    @pytest.mark.parametrize("path", _PATH_IDS)
    def test_an_object_with_no_recorded_owner_is_not_public(
        self, client: TestClient, tokens: dict[Role, str], path: str
    ) -> None:
        # Nothing registered means no owner to match. The safe answer is refusal,
        # not "nobody owns it, so everybody may read it".
        for role in Role:
            response = client.get(f"/api/v1/{path}/{uuid.uuid4()}", headers=auth(tokens[role]))
            assert response.status_code == 404
