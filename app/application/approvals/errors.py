"""Approval persistence errors never expose note content or driver details."""

from app.application.errors import ApplicationError


class ApprovalStoreError(ApplicationError):
    """Review storage is unavailable or its persisted snapshot is invalid."""
