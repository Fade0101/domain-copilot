"""History double: production always uses PostgreSQL."""

from datetime import datetime
from uuid import UUID, uuid4

from app.application.ports.sessions import SessionMessage
from app.application.qa.use_cases import AskResult
from app.domain.auth.value_objects import ResourceType, UserId
from app.domain.sessions import ConversationSession
from tests.support.fakes import FakeOwnershipQuery


class FakeSessionStore:
    def __init__(self, ownership: FakeOwnershipQuery) -> None:
        self.sessions: dict[UUID, ConversationSession] = {}
        self.history: dict[UUID, list[SessionMessage]] = {}
        self.ownership = ownership

    async def add(self, session: ConversationSession) -> None:
        self.sessions[session.id] = session
        self.ownership.register(ResourceType.SESSION, str(session.id), UserId(str(session.user_id)))

    async def list_sessions(
        self, owner_id: UUID, limit: int, offset: int
    ) -> list[ConversationSession]:
        rows = [session for session in self.sessions.values() if session.user_id == owner_id]
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)[
            offset : offset + limit
        ]

    async def append_exchange(
        self, session_id: UUID, owner_id: UUID, question: str, answer: AskResult, now: datetime
    ) -> None:
        assert self.sessions[session_id].user_id == owner_id
        history = self.history.setdefault(session_id, [])
        history.extend(
            [
                SessionMessage(uuid4(), session_id, len(history) + 1, "user", question, None, now),
                SessionMessage(
                    uuid4(), session_id, len(history) + 2, "assistant", answer.answer, answer, now
                ),
            ]
        )

    async def messages(
        self, session_id: UUID, owner_id: UUID, limit: int, offset: int
    ) -> list[SessionMessage]:
        assert self.sessions[session_id].user_id == owner_id
        return self.history.get(session_id, [])[offset : offset + limit]
