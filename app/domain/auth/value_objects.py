"""Immutable value objects for authentication and access control (BRD FR-8).

Frozen dataclasses that validate their own invariants on construction, so any
*existing* instance is guaranteed valid; failures raise
:class:`~app.domain.shared.errors.InvariantViolationError`.

:class:`Role`, :class:`Permission`, and :class:`ResourceType` are the
authoritative encoding of the BRD AC-8.2 permission matrix. The role ->
permission mapping itself lives in :mod:`app.domain.auth.permissions`.

Pure stdlib + the domain error taxonomy -- no framework, ORM, JWT, or password
hashing imports. Those are infrastructure concerns reached through ports, which
is what keeps the role/permission rules testable with no dependencies at all.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.domain.shared.errors import InvariantViolationError

# A deliberate shape check, not RFC 5322 validation: the domain rejects obvious
# nonsense, and deliverability is not a domain concern.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Matches users.email VARCHAR(255) in migration addc7d39b90f.
_MAX_EMAIL_LEN = 255

#: Minimum plaintext password length accepted when *setting* a password.
PASSWORD_MIN_LENGTH = 12

#: bcrypt consumes at most 72 bytes of input. Older bindings silently truncated
#: past that, meaning two different long passwords could verify against the same
#: hash; modern bindings raise instead. The domain refuses anything longer so
#: neither behaviour can ever apply to a stored credential.
PASSWORD_MAX_BYTES = 72


class Role(StrEnum):
    """The three application roles (BRD AC-8.2). There are exactly these three.

    Values match the ``role_enum`` persisted by migration ``addc7d39b90f``
    (``analyst``/``reviewer``/``admin``), so a role round-trips through the
    database unchanged. Roles align with personas PER-01/PER-02/PER-03.
    """

    ANALYST = "analyst"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class Permission(StrEnum):
    """A single named capability checked server-side (BRD AC-8.3).

    Every member traces to a phrase in the BRD AC-8.2 matrix; nothing here is
    invented. Declaring a permission is not the same as implementing the feature
    behind it -- the approval permissions below are *enforced* by this ticket's
    authorization checks, while the approval workflow itself is owned by a later
    ticket. Naming them now gives that ticket a stable target and keeps the
    matrix complete.
    """

    # --- analyst baseline (BRD AC-8.2, PER-01) -----------------------------
    ASK_QUESTION = "ask_question"  # "Ask questions"
    RUN_WORKFLOW = "run_workflow"  # "run workflows"
    VIEW_OWN_RUNS = "view_own_runs"  # "view own runs/jobs/traces"
    VIEW_OWN_JOBS = "view_own_jobs"
    VIEW_OWN_TRACES = "view_own_traces"
    VIEW_OWN_SESSIONS = "view_own_sessions"  # "view own session history"

    # --- reviewer adds (BRD AC-8.2, PER-02) --------------------------------
    APPROVE_CLINICAL_NOTE = "approve_clinical_note"  # "approve/reject/edit clinical notes"
    REJECT_CLINICAL_NOTE = "reject_clinical_note"
    EDIT_CLINICAL_NOTE = "edit_clinical_note"
    VIEW_PENDING_APPROVALS = "view_pending_approvals"  # "view all pending approvals"
    VIEW_ALL_RUNS = "view_all_runs"  # "view all runs"

    # --- admin adds (BRD AC-8.2, PER-03) -----------------------------------
    INGEST_DOCUMENTS = "ingest_documents"  # "ingest documents"
    MANAGE_USERS = "manage_users"  # "manage users"
    MANAGE_ALL_JOBS = "manage_all_jobs"  # "manage all jobs"
    VIEW_SYSTEM_HEALTH = "view_system_health"  # "view system health/observability"
    VIEW_COST_DASHBOARD = "view_cost_dashboard"  # "cost dashboards"


class ResourceType(StrEnum):
    """Object types that carry a server-side owner and are ownership-checked.

    BRD AC-8.4 and SEC-1a require ownership enforcement on runs, jobs, traces,
    sessions, and documents. The per-type rule for who may read *another* user's
    object is in :mod:`app.domain.auth.permissions`.
    """

    RUN = "run"
    JOB = "job"
    TRACE = "trace"
    SESSION = "session"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class UserId:
    """Stable identity of a user, backed by a UUID string.

    This is the *authenticated principal's* identity. It is only ever built from
    a validated JWT subject or a persisted user record -- never from a request
    body, query parameter, or identity header (BRD AC-8.3).
    """

    value: str

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvariantViolationError(
                f"UserId must be a valid UUID string, got {self.value!r}"
            ) from exc

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """A user's login identifier, normalized to lower-case on construction.

    Normalizing here means ``Alice@Example.com`` and ``alice@example.com`` are
    the same account, so the unique index ``ix_users_email`` cannot be sidestepped
    by varying case.
    """

    value: str

    def __post_init__(self) -> None:
        candidate = self.value.strip().lower() if isinstance(self.value, str) else ""
        if not candidate:
            raise InvariantViolationError("EmailAddress must not be empty")
        if len(candidate) > _MAX_EMAIL_LEN:
            raise InvariantViolationError(
                f"EmailAddress must be at most {_MAX_EMAIL_LEN} characters"
            )
        if not _EMAIL_SHAPE.match(candidate):
            raise InvariantViolationError("EmailAddress is not a valid email address")
        # Frozen dataclass: store the normalized form via the documented escape hatch.
        object.__setattr__(self, "value", candidate)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, repr=False)
class Password:
    """A *transient* plaintext password being set, with the password policy applied.

    Constructed only when a credential is created (demo seeding today, user
    management later) and discarded immediately after hashing. It is deliberately
    **not** used on the login path: a failed login must return 401 without
    revealing anything about the password policy, so login passes the raw string
    straight to :meth:`IPasswordHasher.verify`.

    ``__repr__``/``__str__`` are masked so the value cannot reach a log line, an
    error message, or a traceback frame summary by accident.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise InvariantViolationError("Password must not be empty")
        if len(self.value) < PASSWORD_MIN_LENGTH:
            raise InvariantViolationError(
                f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
            )
        if len(self.value.encode("utf-8")) > PASSWORD_MAX_BYTES:
            raise InvariantViolationError(
                f"Password must be at most {PASSWORD_MAX_BYTES} bytes when UTF-8 encoded"
            )

    def __repr__(self) -> str:
        return "Password(value=***)"

    def __str__(self) -> str:
        return "***"
