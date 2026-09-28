"""Commands for the documents use cases.

Commands are plain, framework-free input DTOs carrying primitive values from the
presentation layer into a use case. They are deliberately *not* pydantic models
-- HTTP validation belongs to the presentation schemas; the use case converts
these primitives into validated domain value objects.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegisterDocumentCommand:
    """Inputs required to register a document's metadata."""

    filename: str
    content_hash: str
    metadata: dict[str, str] | None = None
