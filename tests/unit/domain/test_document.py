"""Unit tests for the documents domain (pure -- no framework/DB/LLM)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.documents.entities import Document
from app.domain.documents.value_objects import (
    ContentHash,
    DocumentId,
    DocumentStatus,
    Filename,
)
from app.domain.shared.errors import InvalidStateTransitionError, InvariantViolationError

_VALID_HASH = "a" * 64
_REGISTERED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _register() -> Document:
    return Document.register(
        document_id=DocumentId(str(uuid.UUID(int=1))),
        filename=Filename("guideline.pdf"),
        content_hash=ContentHash(_VALID_HASH),
        registered_at=_REGISTERED_AT,
    )


def test_register_produces_registered_version_one() -> None:
    document = _register()
    assert document.status is DocumentStatus.REGISTERED
    assert document.version == 1
    assert document.registered_at == _REGISTERED_AT


def test_empty_filename_is_rejected() -> None:
    with pytest.raises(InvariantViolationError):
        Filename("   ")


def test_filename_with_path_separator_is_rejected() -> None:
    with pytest.raises(InvariantViolationError):
        Filename("../etc/passwd")


def test_malformed_content_hash_is_rejected() -> None:
    with pytest.raises(InvariantViolationError):
        ContentHash("not-a-real-hash")


def test_content_hash_is_normalized_to_lowercase() -> None:
    assert ContentHash("A" * 64).value == "a" * 64


def test_invalid_document_id_is_rejected() -> None:
    with pytest.raises(InvariantViolationError):
        DocumentId("not-a-uuid")


def test_full_ingestion_transition_chain() -> None:
    document = _register().start_ingestion().mark_ingested()
    assert document.status is DocumentStatus.INGESTED


def test_failed_ingestion_can_be_retried() -> None:
    document = _register().start_ingestion().mark_failed().start_ingestion()
    assert document.status is DocumentStatus.INGESTING


def test_cannot_skip_from_registered_to_ingested() -> None:
    with pytest.raises(InvalidStateTransitionError):
        _register().mark_ingested()


def test_ingested_is_terminal() -> None:
    ingested = _register().start_ingestion().mark_ingested()
    with pytest.raises(InvalidStateTransitionError):
        ingested.start_ingestion()
