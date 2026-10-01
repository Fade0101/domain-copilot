"""Test doubles implementing the application ports.

These fakes let the application layer be tested with no framework, database, or
LLM present (BRD AR-8). They implement the same Protocols the real adapters do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.application.errors import ExpiredTokenError, InvalidTokenError
from app.application.ports.audit import AuditEntry, IAuditSink
from app.application.ports.ownership import IOwnershipQuery
from app.application.ports.passwords import IPasswordHasher
from app.application.ports.repositories import IDocumentRepository, IUserRepository
from app.application.ports.system import IClock, IIdGenerator
from app.application.ports.tokens import (
    ACCESS_TOKEN_TYPE,
    IssuedToken,
    ITokenService,
    TokenClaims,
)
from app.domain.auth.entities import User
from app.domain.auth.value_objects import (
    EmailAddress,
    Password,
    ResourceType,
    Role,
    UserId,
)
from app.domain.documents.entities import Document
from app.domain.documents.value_objects import ContentHash, DocumentId


class FakeDocumentRepository(IDocumentRepository):
    """In-test document store exposing its contents for assertions."""

    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}

    async def add(self, document: Document) -> None:
        self.documents[document.id.value] = document

    async def get_by_id(self, document_id: DocumentId) -> Document | None:
        return self.documents.get(document_id.value)

    async def get_by_content_hash(self, content_hash: ContentHash) -> Document | None:
        for document in self.documents.values():
            if document.content_hash.value == content_hash.value:
                return document
        return None


class FixedClock(IClock):
    """Clock that always returns a preset instant (deterministic tests)."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class SequentialIdGenerator(IIdGenerator):
    """Deterministic id generator producing UUIDs from an incrementing counter."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return str(uuid.UUID(int=self._counter))


# --------------------------------------------------------------------------- #
# Auth fakes (FR-8)
# --------------------------------------------------------------------------- #

#: Prefix marking a :class:`FakePasswordHasher` digest. Chosen to look nothing
#: like a bcrypt digest, so a test that accidentally exercises the real hasher
#: (or vice versa) fails visibly rather than silently passing.
FAKE_HASH_PREFIX = "fake-hash::"


class FakeUserRepository(IUserRepository):
    """In-test user store exposing its contents for assertions."""

    def __init__(self, users: list[User] | None = None) -> None:
        self.users: dict[str, User] = {}
        for user in users or []:
            self.users[user.id.value] = user

    async def add(self, user: User) -> None:
        self.users[user.id.value] = user

    async def get_by_id(self, user_id: UserId) -> User | None:
        return self.users.get(user_id.value)

    async def get_by_email(self, email: EmailAddress) -> User | None:
        for user in self.users.values():
            if user.email.value == email.value:
                return user
        return None


class FakeOwnershipQuery(IOwnershipQuery):
    """In-test ownership store. ``owner_of`` returns ``None`` for unknown objects."""

    def __init__(self) -> None:
        self.owners: dict[tuple[str, str], str] = {}
        self.lookups: list[tuple[str, str]] = []

    def register(self, resource_type: ResourceType, resource_id: str, owner_id: UserId) -> None:
        self.owners[(resource_type.value, resource_id)] = owner_id.value

    async def owner_of(self, resource_type: ResourceType, resource_id: str) -> UserId | None:
        # Recorded so a test can assert that a refusal happened *before* the
        # lookup, which is what keeps an unauthorized caller from learning
        # whether an object exists.
        self.lookups.append((resource_type.value, resource_id))
        owner = self.owners.get((resource_type.value, resource_id))
        return UserId(owner) if owner is not None else None


class FakePasswordHasher(IPasswordHasher):
    """Reversible stand-in for bcrypt: fast, and inspectable in assertions.

    Deliberately *not* a real hash -- a unit test of a use case should not pay for
    a key-derivation function. The real algorithm is covered in
    ``tests/unit/infrastructure/test_password_hasher.py``.
    """

    def __init__(self) -> None:
        self.dummy_verify_calls: list[str] = []

    def hash(self, password: Password) -> str:
        return f"{FAKE_HASH_PREFIX}{password.value}"

    def verify(self, plaintext: str, hashed_password: str) -> bool:
        return hashed_password == f"{FAKE_HASH_PREFIX}{plaintext}"

    def dummy_verify(self, plaintext: str) -> None:
        self.dummy_verify_calls.append(plaintext)


class FakeTokenService(ITokenService):
    """Token service backed by a dict, so tests need no signing or JWT parsing.

    Tokens are opaque strings. ``expire`` and ``corrupt`` let a test produce the
    failure modes the real adapter produces, without constructing a JWT.
    """

    def __init__(self, clock: IClock, ttl_seconds: int = 3600) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._issued: dict[str, TokenClaims] = {}
        self._expired: set[str] = set()
        self._counter = 0

    def issue(self, *, subject: str, role: str) -> IssuedToken:
        self._counter += 1
        token = f"fake-token-{self._counter}"
        issued_at = self._clock.now()
        expires_at = issued_at + timedelta(seconds=self._ttl)
        self._issued[token] = TokenClaims(
            subject=subject,
            role=role,
            issued_at=issued_at,
            expires_at=expires_at,
            token_type=ACCESS_TOKEN_TYPE,
        )
        return IssuedToken(access_token=token, expires_at=expires_at, expires_in_seconds=self._ttl)

    def verify(self, token: str) -> TokenClaims:
        if token in self._expired:
            raise ExpiredTokenError("access token has expired")
        claims = self._issued.get(token)
        if claims is None:
            raise InvalidTokenError("access token is not valid")
        return claims

    def expire(self, token: str) -> None:
        """Mark an already-issued token as expired."""
        self._expired.add(token)

    def forge(self, *, subject: str, role: str) -> str:
        """Return a token that verifies, for a subject that may not exist.

        Used to test what happens when a token outlives its user.
        """
        self._counter += 1
        token = f"forged-token-{self._counter}"
        now = self._clock.now()
        self._issued[token] = TokenClaims(
            subject=subject,
            role=role,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
        )
        return token


class RecordingAuditSink(IAuditSink):
    """Captures audit entries for assertions."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def build_user(
    *,
    user_id: str,
    email: str,
    role: Role,
    password: str = "test-password-1234",
    hasher: IPasswordHasher | None = None,
    created_at: datetime | None = None,
) -> User:
    """Build a :class:`User` whose stored credential is a hash, never plaintext."""
    hasher = hasher or FakePasswordHasher()
    return User(
        id=UserId(user_id),
        email=EmailAddress(email),
        role=role,
        hashed_password=hasher.hash(Password(password)),
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
    )
