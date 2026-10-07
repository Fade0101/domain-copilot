"""Unit tests for deterministic clinical PII/PHI detection and redaction (SEC-2c, BR-06)."""

from __future__ import annotations

import pytest

from app.infrastructure.security.pii import DeterministicPiiRedactor, is_valid_nhs_number


class TestNhsNumberValidation:
    def test_valid_nhs_numbers_pass_modulus_11(self) -> None:
        # Standard test NHS numbers using NHS England Modulus 11 algorithm
        assert is_valid_nhs_number("943 476 5919")
        assert is_valid_nhs_number("943-476-5919")
        assert is_valid_nhs_number("9434765919")

    def test_invalid_check_digit_is_rejected(self) -> None:
        # Check digit should be 9, altered to 8
        assert not is_valid_nhs_number("943 476 5918")
        assert not is_valid_nhs_number("9434765918")

    def test_invalid_length_or_characters_are_rejected(self) -> None:
        assert not is_valid_nhs_number("12345")
        assert not is_valid_nhs_number("abcdefghij")
        assert not is_valid_nhs_number("")


class TestDeterministicPiiRedactor:
    @pytest.fixture
    def redactor(self) -> DeterministicPiiRedactor:
        return DeterministicPiiRedactor()

    def test_redacts_valid_nhs_number(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Patient NHS number is 943 476 5919 for referral."
        assert redactor.contains_pii(text)
        redacted = redactor.redact(text)
        assert redacted == "Patient NHS number is [REDACTED_NHS_NUMBER] for referral."

    def test_does_not_redact_invalid_nhs_number(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Account reference is 943 476 5918 in system."
        assert not redactor.contains_pii(text)
        assert redactor.redact(text) == text

    def test_redacts_ssn(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Social security: 123-45-6789."
        assert redactor.contains_pii(text)
        assert redactor.redact(text) == "Social security: [REDACTED_SSN]."

    def test_redacts_mrn(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Patient chart MRN: AB1234567 admitted yesterday."
        assert redactor.contains_pii(text)
        assert redactor.redact(text) == "Patient chart [REDACTED_MRN] admitted yesterday."

    def test_redacts_email(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Contact clinician at doctor.smith@hospital.nhs.uk for advice."
        assert redactor.contains_pii(text)
        assert redactor.redact(text) == "Contact clinician at [REDACTED_EMAIL] for advice."

    def test_redacts_phone_number(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Direct callback: +44 0207 123 456 or 555-123-4567."
        assert redactor.contains_pii(text)
        redacted = redactor.redact(text)
        assert "[REDACTED_PHONE]" in redacted

    def test_redacts_date_of_birth(self, redactor: DeterministicPiiRedactor) -> None:
        text = "DOB: 15/04/1982 recorded on intake."
        assert redactor.contains_pii(text)
        assert redactor.redact(text) == "[REDACTED_DOB] recorded on intake."

    def test_redacts_multiple_identifiers_in_single_string(
        self, redactor: DeterministicPiiRedactor
    ) -> None:
        text = "Referral for 943 476 5919, DOB: 1980-01-01, contact clin@nhs.net or 555-234-5678."
        redacted = redactor.redact(text)
        assert "[REDACTED_NHS_NUMBER]" in redacted
        assert "[REDACTED_DOB]" in redacted
        assert "[REDACTED_EMAIL]" in redacted
        assert "[REDACTED_PHONE]" in redacted
        assert "943 476 5919" not in redacted
        assert "clin@nhs.net" not in redacted

    def test_redaction_is_idempotent(self, redactor: DeterministicPiiRedactor) -> None:
        text = "Patient 943 476 5919 with email test@example.com"
        redacted_once = redactor.redact(text)
        redacted_twice = redactor.redact(redacted_once)
        assert redacted_once == redacted_twice

    def test_clean_clinical_text_is_unaltered(self, redactor: DeterministicPiiRedactor) -> None:
        text = "The patient presents with symptoms of type 2 diabetes and hypertension."
        assert not redactor.contains_pii(text)
        assert redactor.redact(text) == text
