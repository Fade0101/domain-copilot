"""Unit tests for workflow control limits: iterations, timeouts, and retries (Ticket #17)."""

import pytest

from app.domain.workflow.errors import (
    WorkflowIterationLimitExceededError,
    WorkflowStepTimeoutError,
)
from app.domain.workflow.policy import (
    MAX_STEP_RETRIES,
    MAX_STEP_TIMEOUT_SECONDS,
    MAX_WORKFLOW_ITERATIONS,
    WorkflowPolicy,
)


def test_iteration_limit_boundaries() -> None:
    """Iteration count <= 10 allowed; 11 rejected."""
    policy = WorkflowPolicy()
    assert policy.max_iterations == MAX_WORKFLOW_ITERATIONS == 10

    # 1 to 10 are allowed
    for count in range(1, 11):
        policy.check_iteration(count)

    # 11 raises limit exceeded
    with pytest.raises(WorkflowIterationLimitExceededError) as exc_info:
        policy.check_iteration(11)
    assert "limit of 10 exceeded" in str(exc_info.value)
    assert "attempted 11" in str(exc_info.value)

    # 12 and above also raise
    with pytest.raises(WorkflowIterationLimitExceededError):
        policy.check_iteration(12)


def test_step_timeout_boundaries() -> None:
    """Step duration <= 60.0s allowed; > 60.0s rejected."""
    policy = WorkflowPolicy()
    assert policy.step_timeout_seconds == MAX_STEP_TIMEOUT_SECONDS == 60.0

    # Under limit allowed
    policy.check_timeout(0.0)
    policy.check_timeout(30.5)
    policy.check_timeout(60.0)

    # Over 60.0s rejected
    with pytest.raises(WorkflowStepTimeoutError) as exc_info:
        policy.check_timeout(60.01)
    assert "timed out after 60.01s" in str(exc_info.value)

    with pytest.raises(WorkflowStepTimeoutError):
        policy.check_timeout(120.0)


def test_step_retry_boundaries() -> None:
    """Retries 0, 1, 2 permitted; 3 permitted (total 3 retries); 4 rejected."""
    policy = WorkflowPolicy()
    assert policy.max_step_retries == MAX_STEP_RETRIES == 3

    # Retries 0, 1, 2 permitted (making it retry attempt 1, 2, 3)
    assert policy.is_retry_permitted(0) is True
    assert policy.is_retry_permitted(1) is True
    assert policy.is_retry_permitted(2) is True

    # Attempting a 4th retry is rejected
    assert policy.is_retry_permitted(3) is False
    assert policy.is_retry_permitted(4) is False
