"""Unit tests for the error taxonomies (BRD AR-5).

Asserts the inheritance relationships the HTTP boundary relies on to map errors
by type. Domain-rule violations live under ``DomainError``; orchestration and
configuration failures live under ``ApplicationError``.
"""

from __future__ import annotations

from app.application.errors import (
    ApplicationError,
    ConfigurationError,
    JobNotFoundError,
    PromptNotFoundError,
    PromptValidationError,
    ResourceNotFoundError,
)
from app.domain.shared.errors import (
    ApprovalRequiredError,
    DomainError,
    InsufficientEvidenceError,
    InvalidStateTransitionError,
    InvariantViolationError,
    SafetyCheckFailedError,
)


def test_job_not_found_is_a_resource_not_found_application_error() -> None:
    assert issubclass(JobNotFoundError, ResourceNotFoundError)
    assert issubclass(ResourceNotFoundError, ApplicationError)


def test_prompt_errors_are_configuration_errors() -> None:
    assert issubclass(ConfigurationError, ApplicationError)
    assert issubclass(PromptNotFoundError, ConfigurationError)
    assert issubclass(PromptValidationError, ConfigurationError)


def test_domain_error_seeds_are_domain_errors() -> None:
    for error_type in (
        InvariantViolationError,
        InvalidStateTransitionError,
        InsufficientEvidenceError,
        SafetyCheckFailedError,
        ApprovalRequiredError,
    ):
        assert issubclass(error_type, DomainError)


def test_taxonomies_are_disjoint() -> None:
    assert not issubclass(DomainError, ApplicationError)
    assert not issubclass(ApplicationError, DomainError)
