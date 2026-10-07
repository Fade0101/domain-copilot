"""End-to-end security hardening tests (Ticket #26, OWASP LLM & API controls)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.application.auth.commands import AuthenticateUserCommand
from app.application.clinical_tools.contracts import FinalizeClinicalNoteInput
from app.application.clinical_tools.finalization import ApprovalSnapshot, verified_approved_note
from app.application.documents.ingestion_service import source_media_type
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, Password, ResourceType, Role, UserId
from app.domain.shared.errors import ApprovalRequiredError, InvariantViolationError
from tests.integration.conftest import auth, own


class TestOwnershipEnforcementHardening:
    """Rigorous BOLA / IDOR tests across user-scoped resource routes."""

    @pytest.mark.parametrize(
        ("path", "resource_type"),
        [
            ("runs", ResourceType.RUN),
            ("jobs", ResourceType.JOB),
            ("traces", ResourceType.TRACE),
            ("sessions", ResourceType.SESSION),
        ],
    )
    def test_analyst_cannot_access_unowned_resource(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        path: str,
        resource_type: ResourceType,
    ) -> None:
        other_user = UserId(str(uuid.uuid4()))
        resource_id = str(uuid.uuid4())
        own(resource_type, resource_id, other_user)

        response = client.get(
            f"/api/v1/{path}/{resource_id}",
            headers=auth(tokens[Role.ANALYST]),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "RESOURCE_FORBIDDEN"

    def test_arbitrary_unregistered_id_returns_404(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        unregistered = str(uuid.uuid4())
        response = client.get(
            f"/api/v1/runs/{unregistered}",
            headers=auth(tokens[Role.ANALYST]),
        )
        assert response.status_code == 404


class TestPathTraversalAndUploadHardening:
    """File upload safety: path traversal, NUL bytes, and unsupported extensions."""

    @pytest.mark.parametrize(
        "bad_filename",
        [
            "../../etc/passwd",
            "../secret.pdf",
            "..\\..\\windows\\system32\\calc.exe.pdf",
            "test\x00malicious.pdf",
            "test/malicious.pdf",
            "test\\malicious.pdf",
            ".",
            "..",
            "",
            "   ",
        ],
    )
    def test_source_media_type_rejects_traversal_and_paths(self, bad_filename: str) -> None:
        with pytest.raises(
            InvariantViolationError, match="Supply a plain source filename without a path"
        ):
            source_media_type(bad_filename, "application/pdf")

    def test_source_media_type_rejects_unsupported_extensions(self) -> None:
        with pytest.raises(
            InvariantViolationError, match="Only PDF and Markdown sources are supported"
        ):
            source_media_type("malicious.exe", "application/octet-stream")


class TestFailClosedSafetyBoundary:
    """Fail-closed guarantees at the authoritative clinical finalization boundary."""

    def test_finalization_fails_closed_without_persisted_approval(self) -> None:
        valid_digest = hashlib.sha256(b"draft").hexdigest()
        req = FinalizeClinicalNoteInput(
            workflow_id=uuid.uuid4(),
            approval_id=uuid.uuid4(),
            draft_id=valid_digest,
        )
        with pytest.raises(ApprovalRequiredError, match="Persisted approval is required"):
            verified_approved_note(req, None)

    def test_finalization_fails_closed_when_approval_status_is_not_approved(self) -> None:
        wf_id = uuid.uuid4()
        app_id = uuid.uuid4()
        valid_digest = hashlib.sha256(b"Original note text").hexdigest()
        req = FinalizeClinicalNoteInput(
            workflow_id=wf_id, approval_id=app_id, draft_id=valid_digest
        )
        snapshot = ApprovalSnapshot(
            approval_id=app_id,
            workflow_id=wf_id,
            status="REJECTED",
            original_note="Original note text",
            approved_note="Approved note text",
        )
        with pytest.raises(ApprovalRequiredError, match="status must be exactly APPROVED"):
            verified_approved_note(req, snapshot)


class TestCredentialLoggingHygiene:
    """Ensure credentials, passwords, and tokens never leak into string representations."""

    def test_password_value_object_masks_in_repr(self) -> None:
        raw_secret = "super_secret_password_123"
        pwd = Password(raw_secret)
        assert raw_secret not in repr(pwd)
        assert raw_secret not in str(pwd)
        assert "Password(value=***)" in repr(pwd)

    def test_authenticate_command_masks_password(self) -> None:
        raw_secret = "super_secret_password_123"
        cmd = AuthenticateUserCommand(email="user@example.com", password=raw_secret)
        assert raw_secret not in repr(cmd)
        assert raw_secret not in str(cmd)

    def test_user_entity_masks_password_hash(self) -> None:
        user = User(
            id=UserId(str(uuid.uuid4())),
            email=EmailAddress("user@example.com"),
            role=Role.ANALYST,
            hashed_password="bcrypt_hash_value_here",
            created_at=datetime.now(UTC),
        )
        assert "bcrypt_hash_value_here" not in repr(user)
        assert "bcrypt_hash_value_here" not in str(user)
