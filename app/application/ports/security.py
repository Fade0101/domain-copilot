"""Security ports: clinical PII/PHI redaction and detection (SEC-2c, BR-06)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PiiMatch:
    """A detected sensitive identifier instance."""

    category: str
    text: str
    start: int
    end: int


class IPiiRedactor(Protocol):
    """Port for deterministic detection and redaction of sensitive identifiers.

    Enforces the clinical boundary ensuring standardized patient identifiers
    are neutralized before telemetry, logging, or third-party transfer.
    """

    def redact(self, text: str) -> str:
        """Return the text with all recognized PII/PHI replaced with typed tokens."""
        ...

    def contains_pii(self, text: str) -> bool:
        """Return True if any recognized sensitive identifier is present in text."""
        ...

    def find_matches(self, text: str) -> list[PiiMatch]:
        """Return all detected identifier matches with their categories and spans."""
        ...
