"""Domain errors for the Clinical Workflow (Ticket #17)."""

from app.domain.shared.errors import DomainError


class InvalidWorkflowTransitionError(DomainError):
    """Raised when an illegal workflow state transition is attempted."""


class WorkflowIterationLimitExceededError(DomainError):
    """Raised when cumulative workflow iterations exceed the maximum bound (<= 10)."""


class WorkflowStepTimeoutError(DomainError):
    """Raised when an agent execution step exceeds the bounded timeout (<= 60s)."""


class SafetyCheckRequiredError(DomainError):
    """Raised when an operation attempts to bypass the mandatory Safety Checker stage."""
