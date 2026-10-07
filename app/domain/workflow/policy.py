"""Control limits policy for clinical workflow execution (BRD AC-5.2, AC-5.3, AC-5.4)."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.workflow.errors import (
    WorkflowIterationLimitExceededError,
    WorkflowStepTimeoutError,
)

MAX_WORKFLOW_ITERATIONS = 10
MAX_STEP_TIMEOUT_SECONDS = 60.0
MAX_STEP_RETRIES = 3


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    max_iterations: int = MAX_WORKFLOW_ITERATIONS
    step_timeout_seconds: float = MAX_STEP_TIMEOUT_SECONDS
    max_step_retries: int = MAX_STEP_RETRIES

    def check_iteration(self, iteration_count: int) -> None:
        """Enforce iteration count <= 10.
        
        Boundary:
        iteration_count <= 10 -> allowed
        iteration_count > 10 (i.e. 11) -> WorkflowIterationLimitExceededError
        """
        if iteration_count > self.max_iterations:
            raise WorkflowIterationLimitExceededError(
                f"Workflow iteration limit of {self.max_iterations} exceeded "
                f"(attempted {iteration_count})."
            )

    def check_timeout(self, elapsed_seconds: float) -> None:
        """Enforce execution duration <= 60.0s.
        
        Boundary:
        elapsed_seconds <= 60.0 -> allowed
        elapsed_seconds > 60.0 -> WorkflowStepTimeoutError
        """
        if elapsed_seconds > self.step_timeout_seconds:
            raise WorkflowStepTimeoutError(
                f"Agent step timed out after {elapsed_seconds:.2f}s "
                f"(maximum {self.step_timeout_seconds:.1f}s)."
            )

    def is_retry_permitted(self, retry_count: int) -> bool:
        """Enforce maximum 3 retries per step.
        
        retry_count is the number of previous retries for this step.
        0, 1, 2 -> next retry permitted (making it retry 1, 2, 3)
        >= 3 -> no further retry permitted (4th retry blocked)
        """
        return retry_count < self.max_step_retries
