"""Domain error taxonomy (BRD AR-5).

Every domain-rule violation is expressed as a typed exception deriving from
:class:`DomainError` -- never as a bare ``ValueError``/``AssertionError`` and
never as an error return code. This lets the presentation layer translate
failures to HTTP responses *by type* (see ``app.presentation.api.app``) without
leaking framework or persistence concerns into the domain.

This module establishes the taxonomy plus the failures exercised by the
document-registration slice. The workflow-specific failures named in BRD AR-5
(``InsufficientEvidenceError``, ``SafetyCheckFailedError``,
``ApprovalRequiredError``) are seeded here as anchors so the base class and its
mapping exist now; their raising logic is finalized in the clinical-workflow
tickets. Pure stdlib -- this module imports nothing.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every domain-rule violation.

    Catching :class:`DomainError` catches all domain failures; the more
    specific subclasses below allow precise handling and precise HTTP status
    mapping at the boundary.
    """


class InvariantViolationError(DomainError):
    """A value-object or entity invariant was violated.

    Raised when constructing a value object from invalid data (an empty
    filename, a malformed content hash, ...) or when an entity would otherwise
    be placed in an inconsistent state. Maps to HTTP 422 at the boundary.
    """


class InvalidStateTransitionError(DomainError):
    """An entity was asked to move between two states its lifecycle forbids.

    Maps to HTTP 409 (conflict) at the boundary.
    """


# --- AR-5 seeds -------------------------------------------------------------
# These are raised by later clinical-workflow tickets. They are declared here
# so the taxonomy and its shared base class are established by this ticket and
# downstream code has a stable import target. Do not add raising logic until
# the owning ticket.
class InsufficientEvidenceError(DomainError):
    """Grounded generation lacked sufficient retrieved evidence to answer.

    Contract finalized in the retrieval / grounded-Q&A ticket.
    """


class SafetyCheckFailedError(DomainError):
    """A mandatory clinical safety check did not pass.

    Contract finalized in the Safety Checker ticket.
    """


class ApprovalRequiredError(DomainError):
    """An action requires a persisted human approval that is absent.

    Contract finalized in the approval-workflow ticket.
    """
