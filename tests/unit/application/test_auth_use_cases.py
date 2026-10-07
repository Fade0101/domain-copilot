"""Authentication use cases: login and per-request principal resolution (FR-8).

Two properties get the most attention here because they are the ones an attacker
probes first:

* A login for an unknown account is indistinguishable from a login with the wrong
  password -- same error type, and the same work performed.
* A principal's role comes from the stored user record, not from the token, so a
  token cannot outlive a demotion.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.auth.commands import AuthenticateUserCommand
from app.application.auth.use_cases import (
    AuthenticateUserUseCase,
    ResolvePrincipalUseCase,
)
from app.application.errors import (
    ExpiredTokenError,
    InvalidCredentialsError,
    InvalidTokenError,
    UnknownPrincipalError,
)
from app.application.ports.audit import OUTCOME_ALLOWED, OUTCOME_DENIED
from app.domain.auth.value_objects import Role
from tests.support.fakes import (
    FakePasswordHasher,
    FakeTokenService,
    FakeUserRepository,
    FixedClock,
    RecordingAuditSink,
    build_user,
)

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_ANALYST_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
_PASSWORD = "analyst-password-1234"


class _Fixture:
    """A wired-up use case plus the fakes behind it, for assertions."""

    def __init__(self, role: Role = Role.ANALYST) -> None:
        self.clock = FixedClock(_NOW)
        self.hasher = FakePasswordHasher()
        self.user = build_user(
            user_id=_ANALYST_ID,
            email="analyst@example.com",
            role=role,
            password=_PASSWORD,
            hasher=self.hasher,
        )
        self.users = FakeUserRepository([self.user])
        self.tokens = FakeTokenService(self.clock)
        self.audit = RecordingAuditSink()
        self.login = AuthenticateUserUseCase(
            users=self.users,
            password_hasher=self.hasher,
            token_service=self.tokens,
            audit_sink=self.audit,
            clock=self.clock,
        )
        self.resolve = ResolvePrincipalUseCase(users=self.users, token_service=self.tokens)


class TestAuthenticateUser:
    async def test_correct_credentials_issue_a_token(self) -> None:
        f = _Fixture()
        result = await f.login.execute(
            AuthenticateUserCommand(email="analyst@example.com", password=_PASSWORD)
        )
        assert result.token.access_token
        assert result.token.token_type == "bearer"
        assert result.token.expires_at > _NOW
        assert result.principal.role == "analyst"
        assert result.principal.id == _ANALYST_ID

    async def test_result_never_carries_the_password_hash(self) -> None:
        f = _Fixture()
        result = await f.login.execute(
            AuthenticateUserCommand(email="analyst@example.com", password=_PASSWORD)
        )
        assert not hasattr(result.principal, "hashed_password")
        assert f.user.hashed_password not in repr(result)

    async def test_incorrect_password_is_rejected(self) -> None:
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(
                AuthenticateUserCommand(email="analyst@example.com", password="wrong-password")
            )

    async def test_unknown_email_is_rejected_with_the_same_error(self) -> None:
        # Same error type as a wrong password: the caller cannot tell which failed.
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(
                AuthenticateUserCommand(email="nobody@example.com", password=_PASSWORD)
            )

    async def test_unknown_email_still_performs_a_verification(self) -> None:
        # Without this, "unknown account" returns measurably faster than "wrong
        # password", which enumerates registered users.
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(
                AuthenticateUserCommand(email="nobody@example.com", password="some-password")
            )
        assert f.hasher.dummy_verify_calls == ["some-password"]

    async def test_malformed_email_is_a_401_not_a_422(self) -> None:
        # A login endpoint must not reveal the address format rule, and "that is
        # not a valid email" is not an authentication outcome.
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(AuthenticateUserCommand(email="not-an-email", password=_PASSWORD))

    async def test_email_lookup_is_case_insensitive(self) -> None:
        f = _Fixture()
        result = await f.login.execute(
            AuthenticateUserCommand(email="  ANALYST@Example.COM ", password=_PASSWORD)
        )
        assert result.principal.email == "analyst@example.com"

    async def test_an_overlong_password_is_rejected_not_raised_on(self) -> None:
        # The Password value object caps input at 72 bytes, but a login attempt
        # bypasses that policy check by design, so the hasher must absorb it.
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(
                AuthenticateUserCommand(email="analyst@example.com", password="x" * 200)
            )

    async def test_successful_login_is_audited(self) -> None:
        f = _Fixture()
        await f.login.execute(
            AuthenticateUserCommand(email="analyst@example.com", password=_PASSWORD)
        )
        (entry,) = f.audit.entries
        assert entry.action == "auth.login"
        assert entry.outcome == OUTCOME_ALLOWED
        assert entry.actor_id == _ANALYST_ID
        assert entry.actor_role == "analyst"
        assert entry.occurred_at == _NOW

    async def test_failed_login_is_audited_as_denied(self) -> None:
        f = _Fixture()
        with pytest.raises(InvalidCredentialsError):
            await f.login.execute(
                AuthenticateUserCommand(email="analyst@example.com", password="wrong-password")
            )
        (entry,) = f.audit.entries
        assert entry.outcome == OUTCOME_DENIED
        assert entry.actor_id == _ANALYST_ID

    async def test_audit_entry_carries_no_credential(self) -> None:
        f = _Fixture()
        await f.login.execute(
            AuthenticateUserCommand(email="analyst@example.com", password=_PASSWORD)
        )
        (entry,) = f.audit.entries
        rendered = repr(entry)
        assert _PASSWORD not in rendered
        assert f.user.hashed_password not in rendered

    async def test_command_repr_masks_the_password(self) -> None:
        command = AuthenticateUserCommand(email="analyst@example.com", password=_PASSWORD)
        assert _PASSWORD not in repr(command)
        assert "analyst@example.com" in repr(command)

    async def test_issued_token_repr_masks_the_token(self) -> None:
        f = _Fixture()
        issued = f.tokens.issue(subject=_ANALYST_ID, role="analyst")
        assert issued.access_token not in repr(issued)


class TestResolvePrincipal:
    async def test_a_valid_token_resolves_to_the_stored_user(self) -> None:
        f = _Fixture()
        issued = f.tokens.issue(subject=_ANALYST_ID, role="analyst")
        principal = await f.resolve.execute(issued.access_token)
        assert principal.user_id.value == _ANALYST_ID
        assert principal.email.value == "analyst@example.com"
        assert principal.role is Role.ANALYST

    async def test_the_stored_role_wins_over_the_token_claim(self) -> None:
        # The decisive test for "derive identity from the server-side record". The
        # token is correctly signed and says admin; the stored user is an analyst.
        f = _Fixture()
        token = f.tokens.forge(subject=_ANALYST_ID, role="admin")
        principal = await f.resolve.execute(token)
        assert principal.role is Role.ANALYST

    async def test_a_demotion_takes_effect_without_waiting_for_expiry(self) -> None:
        f = _Fixture(role=Role.ADMIN)
        issued = f.tokens.issue(subject=_ANALYST_ID, role="admin")
        assert (await f.resolve.execute(issued.access_token)).role is Role.ADMIN

        demoted = build_user(
            user_id=_ANALYST_ID,
            email="analyst@example.com",
            role=Role.ANALYST,
            password=_PASSWORD,
            hasher=f.hasher,
        )
        await f.users.add(demoted)
        assert (await f.resolve.execute(issued.access_token)).role is Role.ANALYST

    async def test_an_unknown_token_is_rejected(self) -> None:
        f = _Fixture()
        with pytest.raises(InvalidTokenError):
            await f.resolve.execute("not-a-token-we-issued")

    async def test_an_expired_token_is_rejected(self) -> None:
        f = _Fixture()
        issued = f.tokens.issue(subject=_ANALYST_ID, role="analyst")
        f.tokens.expire(issued.access_token)
        with pytest.raises(ExpiredTokenError):
            await f.resolve.execute(issued.access_token)

    async def test_a_token_whose_subject_is_not_a_uuid_is_a_401(self) -> None:
        # Not a 422: a signed token with a nonsense subject is a token problem,
        # and the caller gets the same answer as for any other bad token.
        f = _Fixture()
        token = f.tokens.forge(subject="admin-id", role="admin")
        with pytest.raises(InvalidTokenError):
            await f.resolve.execute(token)

    async def test_a_token_for_a_deleted_user_is_a_401(self) -> None:
        f = _Fixture()
        token = f.tokens.forge(subject="11111111-1111-4111-8111-111111111111", role="admin")
        with pytest.raises(UnknownPrincipalError):
            await f.resolve.execute(token)

    async def test_expired_token_error_is_an_authentication_error(self) -> None:
        # Both map to 401 at the boundary via the AuthenticationError handler.
        assert issubclass(ExpiredTokenError, InvalidTokenError)
