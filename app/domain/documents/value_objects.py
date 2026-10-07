"""Immutable value objects for the documents aggregate.

Value objects are frozen dataclasses that validate their own invariants on
construction, so any *existing* instance is guaranteed valid. Validation
failures raise :class:`app.domain.shared.errors.InvariantViolationError`.

Pure stdlib + the domain error taxonomy -- no framework, ORM, or SDK imports.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.domain.shared.errors import InvariantViolationError

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILENAME_LEN = 255


@dataclass(frozen=True, slots=True)
class DocumentId:
    """Stable identity of a document, backed by a UUID string."""

    value: str

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvariantViolationError(
                f"DocumentId must be a valid UUID string, got {self.value!r}"
            ) from exc

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ContentHash:
    """SHA-256 content digest used for idempotent registration.

    Normalized to lower-case hex on construction. Two documents sharing a
    content hash are the same corpus artifact (the BRD idempotency rule keys
    registration on this value).
    """

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip().lower() if isinstance(self.value, str) else ""
        if not _SHA256_HEX.match(normalized):
            raise InvariantViolationError("ContentHash must be a 64-character hex SHA-256 digest")
        # Frozen dataclass: store the normalized form via the documented escape hatch.
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Filename:
    """A human-facing document filename (never a filesystem path)."""

    value: str

    def __post_init__(self) -> None:
        candidate = self.value.strip() if isinstance(self.value, str) else ""
        if not candidate:
            raise InvariantViolationError("Filename must not be empty")
        if len(candidate) > _MAX_FILENAME_LEN:
            raise InvariantViolationError(
                f"Filename must be at most {_MAX_FILENAME_LEN} characters"
            )
        if any(sep in candidate for sep in ("/", "\\", "\x00")):
            raise InvariantViolationError("Filename must not contain path separators")
        object.__setattr__(self, "value", candidate)

    def __str__(self) -> str:
        return self.value


class DocumentStatus(StrEnum):
    """Ingestion-readiness status of a document.

    Deliberately distinct from the T7 *job lifecycle*
    (``PENDING -> QUEUED -> STARTED -> COMPLETED | FAILED | CANCELLED``) and the
    clinical *workflow* state machine, per the SYSTEM-DESIGN "key distinction"
    (do not conflate domain workflow states with infrastructure job states).
    This enum describes only whether a document's content is retrievable yet.
    """

    REGISTERED = "REGISTERED"  # metadata stored; ingestion not started
    INGESTING = "INGESTING"  # chunking / embedding in progress
    INGESTED = "INGESTED"  # chunks + embeddings persisted; retrievable
    FAILED = "FAILED"  # ingestion failed; may be retried
