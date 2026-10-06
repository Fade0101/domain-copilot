"""Owner-scoped submission identities using the existing canonical JSON hash."""

from typing import Any
from uuid import UUID

from app.application.evaluation.data import canonical_hash


def submission_key(operation: str, payload: dict[str, Any], version: str, owner: UUID) -> str:
    # Inputs are validated finite JSON before this is called. Preserve string
    # contents and array order; object key order/JSON wire formatting do not matter.
    return "job-v1:" + canonical_hash(
        {"operation": operation, "input": payload, "version": version, "owner": str(owner)}
    )
