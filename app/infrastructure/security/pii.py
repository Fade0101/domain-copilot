"""Deterministic healthcare PII/PHI detection and redaction engine (SEC-2c, BR-06).

Evaluated in ADR-009: In-tree deterministic detection provides reproducible,
offline, zero-network verification for standardized healthcare and personal identifiers
without the heavy runtime footprint or model drift of external NLP pipelines.
"""

from __future__ import annotations

import re
from typing import Final

from app.application.ports.security import IPiiRedactor, PiiMatch

_NHS_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d{3}[-\s]?\d{3}[-\s]?\d{4}|\d{10})\b")

_SSN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"
)

_MRN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:MRN|mrn)[\s#:]+([A-Za-z0-9]{6,12})\b",
    re.IGNORECASE,
)

_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

_PHONE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:\+44\s?\(?0\)?|\(?0\d{4}\)?)\s?\d{3}\s?\d{3}\b|"
    r"\b(?:\+1[-.\s]?)?(?:\(\d{3}\)\s*|\d{3}[.-])\d{3}[.-]\d{4}\b)"
)

_DOB_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:DOB|Date of Birth)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)


def is_valid_nhs_number(candidate: str) -> bool:
    """Validate a 10-digit NHS number using the Modulus 11 algorithm.

    Weights are 10 down to 2 for the first 9 digits. The check digit is
    (11 - remainder) % 11. A remainder resulting in check digit 10 is invalid.
    """
    cleaned = re.sub(r"[-\s]", "", candidate)
    if len(cleaned) != 10 or not cleaned.isdigit():
        return False

    digits = [int(c) for c in cleaned]
    multipliers = range(10, 1, -1)  # 10 down to 2
    total = sum(d * m for d, m in zip(digits[:9], multipliers, strict=False))

    remainder = total % 11
    check_digit = 0 if remainder == 0 else 11 - remainder

    if check_digit == 10:
        return False

    return check_digit == digits[9]


class DeterministicPiiRedactor(IPiiRedactor):
    """Deterministic, rule-based clinical PII/PHI detection and redaction engine."""

    def find_matches(self, text: str) -> list[PiiMatch]:
        matches: list[PiiMatch] = []

        # 1. NHS Number (with Modulus 11 check digit verification)
        for m in _NHS_PATTERN.finditer(text):
            if is_valid_nhs_number(m.group()):
                matches.append(PiiMatch("NHS_NUMBER", m.group(), m.start(), m.end()))

        # 2. SSN
        for m in _SSN_PATTERN.finditer(text):
            matches.append(PiiMatch("SSN", m.group(), m.start(), m.end()))

        # 3. MRN
        for m in _MRN_PATTERN.finditer(text):
            matches.append(PiiMatch("MRN", m.group(), m.start(), m.end()))

        # 4. Email
        for m in _EMAIL_PATTERN.finditer(text):
            matches.append(PiiMatch("EMAIL", m.group(), m.start(), m.end()))

        # 5. Phone
        for m in _PHONE_PATTERN.finditer(text):
            matches.append(PiiMatch("PHONE", m.group(), m.start(), m.end()))

        # 6. DOB
        for m in _DOB_PATTERN.finditer(text):
            matches.append(PiiMatch("DOB", m.group(), m.start(), m.end()))

        # Sort matches by start position in ascending order
        matches.sort(key=lambda x: x.start)
        return matches

    def contains_pii(self, text: str) -> bool:
        return len(self.find_matches(text)) > 0

    def redact(self, text: str) -> str:
        matches = self.find_matches(text)
        if not matches:
            return text

        # Merge overlapping matches if any, preserving the earliest start
        merged: list[PiiMatch] = []
        for match in matches:
            if not merged:
                merged.append(match)
                continue
            last = merged[-1]
            if match.start < last.end:
                # Overlapping match: keep the longer span or earlier
                continue
            merged.append(match)

        # Replace in reverse order so character offsets remain valid
        result = list(text)
        for match in reversed(merged):
            token = f"[REDACTED_{match.category}]"
            result[match.start : match.end] = list(token)

        return "".join(result)
