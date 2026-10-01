"""In-memory user repository adapter.

Interim persistence adapter implementing :class:`IUserRepository` with plain
dicts, alongside :class:`InMemoryDocumentRepository`. The Ticket #6 migration
already defines the ``users`` table, but no SQLAlchemy models or session factory
exist yet (see the Ticket #5 handover notes), so this is what the API runs on for
now. Not durable and not thread/process safe -- a single in-process dev server and
the test suite.

Emails are keyed by their :class:`EmailAddress` value, which is already lowercased
and stripped, so the uniqueness this enforces matches the ``ix_users_email``
unique index that the SQL adapter will rely on.
"""

from __future__ import annotations

from app.application.ports.repositories import IUserRepository
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, UserId


class InMemoryUserRepository(IUserRepository):
    """Dict-backed implementation of the user repository port."""

    def __init__(self) -> None:
        self._by_id: dict[str, User] = {}
        self._id_by_email: dict[str, str] = {}

    async def add(self, user: User) -> None:
        self._by_id[user.id.value] = user
        self._id_by_email[user.email.value] = user.id.value

    async def get_by_id(self, user_id: UserId) -> User | None:
        return self._by_id.get(user_id.value)

    async def get_by_email(self, email: EmailAddress) -> User | None:
        user_id = self._id_by_email.get(email.value)
        if user_id is None:
            return None
        return self._by_id.get(user_id)
