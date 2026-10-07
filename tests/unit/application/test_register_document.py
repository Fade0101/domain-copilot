"""Unit tests for the RegisterDocument use case, using fakes only.

No FastAPI, no database, no real LLM -- proving the application layer runs on
ports alone (BRD AR-1 / AR-8).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.documents.commands import RegisterDocumentCommand
from app.application.documents.use_cases import RegisterDocumentUseCase
from app.domain.shared.errors import InvariantViolationError
from tests.support.fakes import FakeDocumentRepository, FixedClock, SequentialIdGenerator

_VALID_HASH = "a" * 64
_OTHER_HASH = "b" * 64
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _make() -> tuple[RegisterDocumentUseCase, FakeDocumentRepository]:
    repository = FakeDocumentRepository()
    use_case = RegisterDocumentUseCase(
        repository=repository,
        clock=FixedClock(_NOW),
        id_generator=SequentialIdGenerator(),
    )
    return use_case, repository


async def test_registers_a_new_document() -> None:
    use_case, repository = _make()

    result = await use_case.execute(
        RegisterDocumentCommand(filename="guide.pdf", content_hash=_VALID_HASH)
    )

    assert result.created is True
    assert result.document.filename == "guide.pdf"
    assert result.document.status == "REGISTERED"
    assert result.document.version == 1
    assert result.document.registered_at == _NOW
    assert result.document.id == "00000000-0000-0000-0000-000000000001"
    assert len(repository.documents) == 1


async def test_reregistering_same_hash_is_idempotent() -> None:
    use_case, repository = _make()

    first = await use_case.execute(
        RegisterDocumentCommand(filename="guide.pdf", content_hash=_VALID_HASH)
    )
    second = await use_case.execute(
        RegisterDocumentCommand(filename="different-name.pdf", content_hash=_VALID_HASH)
    )

    assert second.created is False
    assert second.document.id == first.document.id
    assert len(repository.documents) == 1


async def test_idempotent_reregister_does_not_overwrite_metadata() -> None:
    use_case, _ = _make()

    await use_case.execute(
        RegisterDocumentCommand(
            filename="guide.pdf", content_hash=_VALID_HASH, metadata={"source": "original"}
        )
    )
    second = await use_case.execute(
        RegisterDocumentCommand(
            filename="guide.pdf", content_hash=_VALID_HASH, metadata={"source": "changed"}
        )
    )

    assert second.document.metadata == {"source": "original"}


async def test_distinct_hashes_create_distinct_documents() -> None:
    use_case, repository = _make()

    await use_case.execute(RegisterDocumentCommand(filename="a.pdf", content_hash=_VALID_HASH))
    await use_case.execute(RegisterDocumentCommand(filename="b.pdf", content_hash=_OTHER_HASH))

    assert len(repository.documents) == 2


async def test_invalid_content_hash_raises_domain_error() -> None:
    use_case, _ = _make()

    with pytest.raises(InvariantViolationError):
        await use_case.execute(
            RegisterDocumentCommand(filename="guide.pdf", content_hash="bad-hash")
        )
