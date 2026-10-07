"""PostgreSQL user repository (BRD FR-8).

The server-side identity of record, made durable. Explicit statements against the
``users`` table that migration ``addc7d39b90f`` defines -- no ORM mapping, because
Ticket #6 owns that and this needs three statements.

Reading the role from here on every request is what makes it authoritative: a role
changed in the database takes effect on the next request, without waiting for
outstanding tokens to expire.

Making this durable also makes demo seeding idempotent **across restarts** rather
than only within a process: the second run finds the rows the first one wrote.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.repositories import IUserRepository
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, Role, UserId

_SELECT_COLUMNS = "id, email, hashed_password, role, created_at"

_INSERT_USER = text(
    """
    INSERT INTO users (id, email, hashed_password, role, created_at)
    VALUES (:id, :email, :hashed_password, :role, :created_at)
    """
)

_SELECT_BY_ID = text(f"SELECT {_SELECT_COLUMNS} FROM users WHERE id = :id")

# Matches the ix_users_email unique index. EmailAddress has already lowercased and
# stripped the value, so a plain equality comparison is correct and uses the index.
_SELECT_BY_EMAIL = text(f"SELECT {_SELECT_COLUMNS} FROM users WHERE email = :email")


class SqlUserRepository(IUserRepository):
    """Stores and loads :class:`User` records in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, user: User) -> None:
        async with self._session_factory() as session:
            await session.execute(
                _INSERT_USER,
                {
                    "id": UserId(user.id.value).value,
                    "email": user.email.value,
                    # Already a digest. The entity rejects an empty one, and no
                    # plaintext password reaches this layer.
                    "hashed_password": user.hashed_password,
                    "role": user.role.value,
                    "created_at": user.created_at,
                },
            )
            await session.commit()

    async def get_by_id(self, user_id: UserId) -> User | None:
        return await self._fetch_one(_SELECT_BY_ID, {"id": user_id.value})

    async def get_by_email(self, email: EmailAddress) -> User | None:
        return await self._fetch_one(_SELECT_BY_EMAIL, {"email": email.value})

    async def _fetch_one(self, statement: Any, params: dict[str, Any]) -> User | None:
        async with self._session_factory() as session:
            result = await session.execute(statement, params)
            row = result.mappings().one_or_none()

        if row is None:
            return None
        return self._to_entity(row)

    @staticmethod
    def _to_entity(row: Any) -> User:
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            # The column is TIMESTAMPTZ, but a driver returning a naive value
            # would otherwise produce a User whose timestamps cannot be compared
            # against the aware ones the clock port yields.
            created_at = created_at.replace(tzinfo=UTC)
        return User(
            id=UserId(str(row["id"])),
            email=EmailAddress(row["email"]),
            role=Role(row["role"]),
            hashed_password=row["hashed_password"],
            created_at=created_at,
        )
