"""Durable snapshots must preserve every agent field without upgrading safety."""

import copy

import pytest

from app.application.approvals.errors import ApprovalStoreError
from app.domain.shared.errors import InvariantViolationError
from app.infrastructure.persistence.sql.approval_store import MAX_REVIEW_BYTES, ReviewSnapshotCodec
from tests.support.approval_fixtures import review_snapshot


@pytest.mark.parametrize(
    "kind", ["safe", "flagged", "unsupported", "capacity", "tool_error", "timeout"]
)
def test_complete_agent_contracts_survive_json_storage(kind: str) -> None:
    snapshot = review_snapshot(kind=kind)
    codec = ReviewSnapshotCodec()
    encoded = codec.encode(snapshot)
    restored = codec.decode(copy.deepcopy(encoded))
    assert restored == snapshot
    assert type(restored) is type(snapshot)


@pytest.mark.parametrize("corruption", ["version", "identity", "digest", "claim", "boolean"])
def test_corrupt_persisted_snapshot_fails_closed(corruption: str) -> None:
    codec = ReviewSnapshotCodec()
    payload = codec.encode(review_snapshot())
    if corruption == "version":
        payload["schema_version"] = 2
    elif corruption == "identity":
        payload["draft"]["workflow_id"] = "00000000-0000-0000-0000-000000000000"
    elif corruption == "digest":
        payload["draft"]["note"] = "Content replaced after review"
    elif corruption == "claim":
        payload["draft"]["asserted_claims"][0]["detail"] = "Unsupported replacement"
    else:
        payload["draft"]["can_proceed"] = "true"
    with pytest.raises(ApprovalStoreError):
        codec.decode(payload)


def test_oversized_snapshot_is_refused_without_truncating_clinical_text() -> None:
    snapshot = review_snapshot()
    snapshot.draft.metadata["too_large"] = "x" * MAX_REVIEW_BYTES
    with pytest.raises(InvariantViolationError):
        ReviewSnapshotCodec().encode(snapshot)
    preserved = snapshot.draft.metadata["too_large"]
    assert isinstance(preserved, str) and len(preserved) == MAX_REVIEW_BYTES
