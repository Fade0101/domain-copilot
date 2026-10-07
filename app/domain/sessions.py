"""Conversation metadata; sessions are private to their recorded owner."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.shared.errors import InvariantViolationError


@dataclass(frozen=True, slots=True)
class ConversationSession:
    id: UUID
    user_id: UUID
    title: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.title.strip() or len(self.title) > 255:
            raise InvariantViolationError("Session title must contain 1 to 255 characters.")
