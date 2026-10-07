"""One public envelope for application, validation and HTTP errors."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str = Field(description="Safe human-readable explanation; never raw server details.")
    code: str = Field(description="Stable machine-readable error code.", pattern=r"^[A-Z_]+$")


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        400: "DOMAIN_ERROR, APPLICATION_ERROR or HTTP_ERROR: invalid operation.",
        401: "NOT_AUTHENTICATED. WWW-Authenticate: Bearer.",
        403: "PERMISSION_DENIED or RESOURCE_FORBIDDEN.",
        404: "RESOURCE_NOT_FOUND or HTTP_ERROR.",
        405: "HTTP_ERROR: method not allowed.",
        409: "INVALID_STATE_TRANSITION: conflict with persisted state.",
        413: "UPLOAD_TOO_LARGE.",
        422: "VALIDATION_ERROR or INVARIANT_VIOLATION: invalid input.",
        500: "INTERNAL_ERROR or CONFIGURATION_ERROR. Static, sanitized detail.",
        503: "JOB_STORE_UNAVAILABLE, KNOWLEDGE_UNAVAILABLE, APPROVAL_STORE_UNAVAILABLE "
        "or HISTORY_STORE_UNAVAILABLE. Static, sanitized detail.",
    }.items()
}
