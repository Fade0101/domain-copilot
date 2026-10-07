"""Authentication use cases: exchanging credentials for a token, and a token for a principal.

Both depend only on ports. Neither knows that bcrypt hashes passwords or that the
token is a JWT.

The security-relevant decisions live here rather than in the adapters:

* A failed login is indistinguishable from an unknown account -- same error, and
  the same amount of work done (see :meth:`IPasswordHasher.dummy_verify`).
* A token's ``role`` claim is *not* the authority. Every principal is rebuilt from
  the stored user record, so revoking a role or deleting an account takes effect
  on the next request instead of whenever the outstanding tokens expire.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.auth.commands import AuthenticateUserCommand
from app.application.auth.context import Principal
from app.application.auth.dto import AccessTokenView, PrincipalView
from app.application.errors import (
    InvalidCredentialsError,
    InvalidTokenError,
    UnknownPrincipalError,
)
from app.application.ports.audit import (
    OUTCOME_ALLOWED,
    OUTCOME_DENIED,
    AuditEntry,
    IAuditSink,
)
from app.application.ports.passwords import IPasswordHasher
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock
from app.application.ports.tokens import ITokenService
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, UserId
from app.domain.shared.errors import InvariantViolationError

_UNKNOWN_ACTOR = "unknown"
_LOGIN_ACTION = "auth.login"


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    """Outcome of a successful :class:`AuthenticateUserUseCase` execution."""

    token: AccessTokenView
    principal: PrincipalView


class AuthenticateUserUseCase:
    """Verify an email/password pair and issue an access token (BRD AC-8.1)."""

    def __init__(
        self,
        users: IUserRepository,
        password_hasher: IPasswordHasher,
        token_service: ITokenService,
        audit_sink: IAuditSink,
        clock: IClock,
    ) -> None:
        self._users = users
        self._hasher = password_hasher
        self._tokens = token_service
        self._audit = audit_sink
        self._clock = clock

    async def execute(self, command: AuthenticateUserCommand) -> AuthenticationResult:
        user = await self._lookup(command.email)

        if user is None:
            # No account. Still pay the cost of a verification so that the
            # response time does not reveal whether the email is registered.
            self._hasher.dummy_verify(command.password)
            await self._record(actor_id=_UNKNOWN_ACTOR, actor_role=_UNKNOWN_ACTOR, allowed=False)
            raise InvalidCredentialsError("invalid email or password")

        if not self._hasher.verify(command.password, user.hashed_password):
            await self._record(actor_id=user.id.value, actor_role=user.role.value, allowed=False)
            raise InvalidCredentialsError("invalid email or password")

        issued = self._tokens.issue(subject=user.id.value, role=user.role.value)
        await self._record(actor_id=user.id.value, actor_role=user.role.value, allowed=True)

        return AuthenticationResult(
            token=AccessTokenView.from_issued_token(issued),
            principal=PrincipalView.from_principal(Principal.from_user(user)),
        )

    async def _lookup(self, raw_email: str) -> User | None:
        """Resolve ``raw_email`` to a user, treating a malformed address as no match.

        Building an :class:`EmailAddress` normally raises
        :class:`InvariantViolationError` (422) for a bad shape. On a login
        endpoint that would both leak the validation rule and answer with the
        wrong status, so a malformed address is simply an address nobody has.
        """
        try:
            email = EmailAddress(raw_email)
        except InvariantViolationError:
            return None
        return await self._users.get_by_email(email)

    async def _record(self, *, actor_id: str, actor_role: str, allowed: bool) -> None:
        await self._audit.record(
            AuditEntry(
                actor_id=actor_id,
                actor_role=actor_role,
                action=_LOGIN_ACTION,
                outcome=OUTCOME_ALLOWED if allowed else OUTCOME_DENIED,
                occurred_at=self._clock.now(),
            )
        )


class ResolvePrincipalUseCase:
    """Turn a bearer token into the authenticated :class:`Principal` (SDD A.5.1).

    Called on every protected request. The token establishes *which* user is
    claiming to call; the stored record establishes what that user may do.
    """

    def __init__(self, users: IUserRepository, token_service: ITokenService) -> None:
        self._users = users
        self._tokens = token_service

    async def execute(self, token: str) -> Principal:
        # Raises InvalidTokenError / ExpiredTokenError (401) for a bad signature,
        # a malformed token, a wrong issuer/audience/type, or a missing claim.
        claims = self._tokens.verify(token)

        try:
            user_id = UserId(claims.subject)
        except InvariantViolationError as exc:
            # A correctly signed token whose subject is not one of our ids: this
            # is a token problem, not a domain problem, so answer 401 not 422.
            raise InvalidTokenError("token subject is not a valid user id") from exc

        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UnknownPrincipalError("token subject does not resolve to a user")

        # The role claim is informational. The stored role wins, so a role change
        # or a demotion does not wait for the old token to expire.
        return Principal.from_user(user)
