"""Safe, typed failures at the clinical tool boundary."""

from enum import StrEnum

from app.application.errors import ApplicationError, PermissionDeniedError
from app.domain.shared.errors import (
    ApprovalRequiredError,
    InvalidStateTransitionError,
    InvariantViolationError,
)


class ToolInputError(InvariantViolationError):
    """Arguments do not satisfy the named tool's contract."""


class ToolOutputError(ApplicationError):
    """A tool produced an invalid result; it must not reach the caller."""


class ToolPermissionError(PermissionDeniedError):
    """The server-bound agent scope does not grant this tool."""


class UnknownToolError(ApplicationError):
    """The requested name is outside the fixed set of six tools."""


class ApprovalMismatchError(ApprovalRequiredError):
    """The persisted approval does not belong to the requested workflow/draft."""


class FinalNoteConflictError(InvalidStateTransitionError):
    """A workflow already has a different final note; overwriting is forbidden."""


class ClinicalToolStoreError(ApplicationError):
    """Authoritative final-note storage is unavailable."""


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    FINAL_NOTE_CONFLICT = "FINAL_NOTE_CONFLICT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    INVALID_TOOL_OUTPUT = "INVALID_TOOL_OUTPUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"


ERROR_MESSAGES = {
    ToolErrorCode.INVALID_ARGUMENTS: "Invalid clinical tool arguments",
    ToolErrorCode.NOT_AUTHENTICATED: "Authentication is required",
    ToolErrorCode.PERMISSION_DENIED: "Clinical tool execution is not permitted",
    ToolErrorCode.RESOURCE_NOT_FOUND: "The workflow was not found",
    ToolErrorCode.UNKNOWN_TOOL: "Unknown clinical tool",
    ToolErrorCode.APPROVAL_REQUIRED: "A persisted approved clinical note is required",
    ToolErrorCode.APPROVAL_MISMATCH: "Approval does not match the workflow and draft",
    ToolErrorCode.FINAL_NOTE_CONFLICT: "The workflow already has a different final note",
    ToolErrorCode.TOOL_UNAVAILABLE: "The clinical tool is temporarily unavailable",
    ToolErrorCode.INVALID_TOOL_OUTPUT: "The clinical tool returned an invalid result",
    ToolErrorCode.TOOL_EXECUTION_FAILED: "Clinical tool execution failed",
}
