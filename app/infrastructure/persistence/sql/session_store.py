"""PostgreSQL session history using the existing sessions table and connection pool."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.errors import ResourceNotFoundError, ResourceOwnershipError
from app.application.ports.sessions import SessionMessage
from app.application.qa.use_cases import AskResult
from app.application.retrieval.dto import Citation
from app.application.sessions import HistoryStoreUnavailableError
from app.domain.sessions import ConversationSession
from app.infrastructure.persistence.models import SessionMessageModel, SessionModel


class PostgresSessionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except SQLAlchemyError as exc:
            raise HistoryStoreUnavailableError("History storage is unavailable.") from exc

    async def add(self, session: ConversationSession) -> None:
        async with self._transaction() as transaction:
            transaction.add(SessionModel(**asdict(session)))

    async def list_sessions(
        self, owner_id: UUID, limit: int, offset: int
    ) -> list[ConversationSession]:
        async with self._transaction() as session:
            rows = await session.scalars(
                sa.select(SessionModel)
                .where(SessionModel.user_id == owner_id)
                .order_by(SessionModel.created_at.desc(), SessionModel.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [
                ConversationSession(row.id, row.user_id, row.title, row.created_at) for row in rows
            ]

    async def _owned(
        self, session: AsyncSession, session_id: UUID, owner_id: UUID, *, lock: bool = False
    ) -> None:
        query = sa.select(SessionModel).where(SessionModel.id == session_id)
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        if row is None:
            raise ResourceNotFoundError("Session not found.")
        if row.user_id != owner_id:
            raise ResourceOwnershipError("Session belongs to another user.")

    async def append_exchange(
        self, session_id: UUID, owner_id: UUID, question: str, answer: AskResult, now: datetime
    ) -> None:
        async with self._transaction() as session:
            await self._owned(session, session_id, owner_id, lock=True)
            last = (
                await session.scalar(
                    sa.select(sa.func.max(SessionMessageModel.sequence)).where(
                        SessionMessageModel.session_id == session_id
                    )
                )
                or 0
            )
            session.add_all(
                [
                    SessionMessageModel(
                        id=uuid4(),
                        session_id=session_id,
                        sequence=last + 1,
                        role="user",
                        content=question,
                        answer=None,
                        created_at=now,
                    ),
                    SessionMessageModel(
                        id=uuid4(),
                        session_id=session_id,
                        sequence=last + 2,
                        role="assistant",
                        content=answer.answer,
                        answer=json.loads(json.dumps(asdict(answer), default=str)),
                        created_at=now,
                    ),
                ]
            )

    async def messages(
        self, session_id: UUID, owner_id: UUID, limit: int, offset: int
    ) -> list[SessionMessage]:
        async with self._transaction() as session:
            await self._owned(session, session_id, owner_id)
            rows = await session.scalars(
                sa.select(SessionMessageModel)
                .where(SessionMessageModel.session_id == session_id)
                .order_by(SessionMessageModel.sequence)
                .limit(limit)
                .offset(offset)
            )
            result = []
            for row in rows:
                answer = None
                if row.answer is not None:
                    data = row.answer
                    citations = tuple(
                        Citation(
                            document_id=UUID(item["document_id"]),
                            chunk_id=UUID(item["chunk_id"]),
                            **{
                                key: value
                                for key, value in item.items()
                                if key not in {"document_id", "chunk_id"}
                            },
                        )
                        for item in data["citations"]
                    )
                    answer = AskResult(data["answer"], citations, data["refused"], data["trace_id"])
                result.append(
                    SessionMessage(
                        row.id,
                        row.session_id,
                        row.sequence,
                        "user" if row.role == "user" else "assistant",
                        row.content,
                        answer,
                        row.created_at,
                    )
                )
            return result
