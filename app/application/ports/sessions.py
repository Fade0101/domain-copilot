"""Durable session history. A question and its grounded outcome commit together."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.application.qa.use_cases import AskResult
from app.domain.sessions import ConversationSession


@dataclass(frozen=True, slots=True)
class SessionMessage:
    id: UUID
    session_id: UUID
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    answer: AskResult | None
    created_at: datetime


class ISessionStore(Protocol):
    async def add(self, session: ConversationSession) -> None: ...

    async def list_sessions(
        self, owner_id: UUID, limit: int, offset: int
    ) -> list[ConversationSession]: ...

    async def append_exchange(
        self, session_id: UUID, owner_id: UUID, question: str, answer: AskResult, now: datetime
    ) -> None:
        """Lock the session, recheck ownership, atomically append two ordered messages."""
        ...

    async def messages(
        self, session_id: UUID, owner_id: UUID, limit: int, offset: int
    ) -> list[SessionMessage]: ...
