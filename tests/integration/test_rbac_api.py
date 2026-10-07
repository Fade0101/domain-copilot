"""The RBAC matrix enforced over real HTTP (BRD AC-8.2, AC-8.3).

The unit tests in ``tests/unit/domain/test_permissions.py`` assert the matrix
itself, exhaustively. This file asserts that the matrix is what actually decides
real requests -- that enforcement is server-side and on every request, not advice
a client could ignore.

The endpoints used are the ones Ticket #5 legitimately has:

* ``POST /api/v1/documents`` is gated on ``INGEST_DOCUMENTS``, an admin-only
  capability, so it demonstrates an admin operation being refused to the other two
  roles.
* The ownership-checked reads demonstrate the reviewer and admin cross-user
  grants, which differ per resource type.

No endpoint is invented to exercise a permission. The approval capabilities
(``approve_clinical_note`` and friends) exist in the matrix and are asserted at
the unit level, but the approval endpoints belong to Ticket #19 and are not
created here.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.domain.auth.value_objects import ResourceType, Role, UserId
from tests.integration.conftest import auth, own

_DOCUMENTS_URL = "/api/v1/documents"
_ME_URL = "/api/v1/auth/me"


def _document_body() -> dict[str, str]:
    # A unique content hash per call: registration is idempotent by hash, and a
    # repeat would return 200 instead of 201 and muddy the assertion.
    return {"filename": "guideline.pdf", "content_hash": uuid.uuid4().hex * 2}


class TestAdminOnlyOperation:
    """Document ingestion: admin only, per AC-8.2 PER-03."""

    def test_an_unauthenticated_request_is_401(self, client: TestClient) -> None:
        response = client.post(_DOCUMENTS_URL, json=_document_body())
        assert response.status_code == 401
        assert response.json()["code"] == "NOT_AUTHENTICATED"

    def test_an_analyst_is_403(self, client: TestClient, tokens: dict[Role, str]) -> None:
        response = client.post(
            _DOCUMENTS_URL, json=_document_body(), headers=auth(tokens[Role.ANALYST])
        )
        assert response.status_code == 403
        assert response.json()["code"] == "PERMISSION_DENIED"

    def test_a_reviewer_is_also_403(self, client: TestClient, tokens: dict[Role, str]) -> None:
        # Reviewer is not a partial admin: the extra reviewer grants do not include
        # ingestion.
        response = client.post(
            _DOCUMENTS_URL, json=_document_body(), headers=auth(tokens[Role.REVIEWER])
        )
        assert response.status_code == 403

    def test_an_admin_succeeds(self, client: TestClient, tokens: dict[Role, str]) -> None:
        response = client.post(
            _DOCUMENTS_URL, json=_document_body(), headers=auth(tokens[Role.ADMIN])
        )
        assert response.status_code == 201

    def test_a_403_reveals_nothing_beyond_the_refusal(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        response = client.post(
            _DOCUMENTS_URL, json=_document_body(), headers=auth(tokens[Role.ANALYST])
        )
        assert response.json() == {
            "detail": "Insufficient permissions",
            "code": "PERMISSION_DENIED",
        }

    def test_authorization_is_checked_before_the_body_is_processed(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        # An analyst sending an invalid body still gets 403, not 422: the analyst
        # must not be able to use validation responses to probe the endpoint.
        response = client.post(
            _DOCUMENTS_URL,
            json={"filename": "x", "content_hash": "not-a-hash"},
            headers=auth(tokens[Role.ANALYST]),
        )
        assert response.status_code == 403

    def test_the_check_runs_on_every_request_not_just_the_first(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        for _ in range(3):
            assert (
                client.post(
                    _DOCUMENTS_URL, json=_document_body(), headers=auth(tokens[Role.ANALYST])
                ).status_code
                == 403
            )


class TestAnalystOperations:
    """What an analyst *is* allowed to do (PER-01)."""

    def test_an_analyst_can_read_its_own_identity(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        assert client.get(_ME_URL, headers=auth(tokens[Role.ANALYST])).status_code == 200

    @pytest.mark.parametrize(
        ("resource_type", "path"),
        [
            (ResourceType.RUN, "runs"),
            (ResourceType.JOB, "jobs"),
            (ResourceType.TRACE, "traces"),
            (ResourceType.SESSION, "sessions"),
        ],
    )
    def test_an_analyst_can_read_each_of_its_own_resources(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        user_ids: dict[Role, UserId],
        resource_type: ResourceType,
        path: str,
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(resource_type, resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code == 200
        assert response.json()["owner_id"] == user_ids[Role.ANALYST].value


class TestReviewerOperations:
    """The reviewer tier adds cross-user run visibility (PER-02)."""

    def test_a_reviewer_may_read_another_users_run(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId]
    ) -> None:
        run_id = str(uuid.uuid4())
        own(ResourceType.RUN, run_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/runs/{run_id}", headers=auth(tokens[Role.REVIEWER]))
        assert response.status_code == 200
        # Allowed by a documented grant, not by being treated as the owner.
        assert response.json()["owner_id"] == user_ids[Role.ANALYST].value

    def test_an_analyst_may_not(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId]
    ) -> None:
        run_id = str(uuid.uuid4())
        own(ResourceType.RUN, run_id, user_ids[Role.REVIEWER])
        response = client.get(f"/api/v1/runs/{run_id}", headers=auth(tokens[Role.ANALYST]))
        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("resource_type", "path"), [(ResourceType.JOB, "jobs"), (ResourceType.TRACE, "traces")]
    )
    def test_a_reviewer_does_not_inherit_the_admin_grants(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        user_ids: dict[Role, UserId],
        resource_type: ResourceType,
        path: str,
    ) -> None:
        # "manage all jobs" and observability are admin-only, so a reviewer's
        # cross-user reach stops at runs.
        resource_id = str(uuid.uuid4())
        own(resource_type, resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.REVIEWER]))
        assert response.status_code == 403


class TestAdminOperations:
    """The admin tier adds job management and observability (PER-03)."""

    @pytest.mark.parametrize(
        ("resource_type", "path"),
        [
            (ResourceType.RUN, "runs"),
            (ResourceType.JOB, "jobs"),
            (ResourceType.TRACE, "traces"),
        ],
    )
    def test_an_admin_may_read_another_users_runs_jobs_and_traces(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        user_ids: dict[Role, UserId],
        resource_type: ResourceType,
        path: str,
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(resource_type, resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[Role.ADMIN]))
        assert response.status_code == 200

    def test_even_an_admin_may_not_read_another_users_session(
        self, client: TestClient, tokens: dict[Role, str], user_ids: dict[Role, UserId]
    ) -> None:
        # AC-8.2 grants every role "view own session history" and widens it for
        # nobody, so admin access here would be a privilege the matrix never gave.
        session_id = str(uuid.uuid4())
        own(ResourceType.SESSION, session_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/sessions/{session_id}", headers=auth(tokens[Role.ADMIN]))
        assert response.status_code == 403


#: (path, role) -> expected status for an object owned by the *analyst*.
_EXPECTED: dict[tuple[str, Role], int] = {
    ("runs", Role.ANALYST): 200,  # owner
    ("runs", Role.REVIEWER): 200,  # view all runs
    ("runs", Role.ADMIN): 200,  # view all runs
    ("jobs", Role.ANALYST): 200,  # owner
    ("jobs", Role.REVIEWER): 403,
    ("jobs", Role.ADMIN): 200,  # manage all jobs
    ("traces", Role.ANALYST): 200,  # owner
    ("traces", Role.REVIEWER): 403,
    ("traces", Role.ADMIN): 200,  # observability
    ("sessions", Role.ANALYST): 200,  # owner
    ("sessions", Role.REVIEWER): 403,
    ("sessions", Role.ADMIN): 403,
}

_TYPES = {
    "runs": ResourceType.RUN,
    "jobs": ResourceType.JOB,
    "traces": ResourceType.TRACE,
    "sessions": ResourceType.SESSION,
}


class TestTheFullMatrix:
    """One table, asserted end to end over HTTP."""

    @pytest.mark.parametrize(("path", "role"), list(_EXPECTED))
    def test_matrix_cell(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        user_ids: dict[Role, UserId],
        path: str,
        role: Role,
    ) -> None:
        resource_id = str(uuid.uuid4())
        own(_TYPES[path], resource_id, user_ids[Role.ANALYST])
        response = client.get(f"/api/v1/{path}/{resource_id}", headers=auth(tokens[role]))
        assert response.status_code == _EXPECTED[(path, role)]
