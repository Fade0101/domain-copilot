"""JWT access-token service (implements :class:`ITokenService`; BRD AC-8.1).

The only module that imports a JWT library.

Every validation this performs is one an attacker would otherwise get past:

* **Signature**, against the configured secret.
* **A fixed algorithm allow-list.** ``algorithms=[self._algorithm]`` is what stops
  algorithm-confusion: a token whose header says ``{"alg": "none"}`` -- or ``RS256``
  where the "public key" is our HMAC secret -- is refused before its claims are
  read. Never widen this to the list of algorithms PyJWT supports.
* **Expiry and issued-at**, via the ``exp``/``iat`` claims.
* **Issuer and audience**, so a token minted by another service, or for another
  service, is not accepted here.
* **Required claims.** A correctly signed token missing ``role`` or ``sub`` is
  rejected rather than defaulted.
* **Token type**, so a future refresh token can never be replayed as an access
  token.
* **Role shape**, so a signed token naming a role this system does not define is
  refused rather than carried into an authorization decision.

Time is judged by the injected :class:`IClock`, not by the library. PyJWT's
``decode`` reads the real system clock and offers no way to pass in "now", so its
``exp``/``iat`` checks are switched off and performed here instead. That keeps one
time source for both issuing and verifying -- which is what makes expiry testable
by moving a fixed clock forward rather than by sleeping through a real token
lifetime -- and it is a comparison of two numbers, not a reimplementation of
anything cryptographic. Signature, algorithm, issuer, audience, and required-claim
checks all remain PyJWT's.

Errors raised here are the application's own :class:`InvalidTokenError` and
:class:`ExpiredTokenError` with fixed messages. The library's exception text is
attached as ``__cause__`` for a server-side traceback but never becomes the
message, and no code path here puts the token itself into an error or a log
record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import uuid4

import jwt

from app.application.errors import (
    ConfigurationError,
    ExpiredTokenError,
    InvalidTokenError,
)
from app.application.ports.system import IClock
from app.application.ports.tokens import (
    ACCESS_TOKEN_TYPE,
    IssuedToken,
    ITokenService,
    TokenClaims,
)
from app.domain.auth.value_objects import Role

#: Payload claim holding the token's purpose. Named ``token_type`` rather than
#: ``typ`` so it cannot be confused with the JOSE header parameter of that name.
CLAIM_TOKEN_TYPE: Final = "token_type"
CLAIM_ROLE: Final = "role"

_REQUIRED_CLAIMS: Final = ["sub", "exp", "iat", "iss", "aud", CLAIM_ROLE, CLAIM_TOKEN_TYPE]

#: HMAC only. Adding an asymmetric algorithm means introducing key management,
#: which is not in this ticket's scope.
_SUPPORTED_ALGORITHMS: Final = frozenset({"HS256", "HS384", "HS512"})

#: A shorter secret than this does not provide the security margin HMAC-SHA256
#: assumes, so a misconfiguration is refused at startup rather than at runtime.
MIN_SECRET_LENGTH: Final = 32

#: Tolerance for an ``iat`` that sits slightly ahead of the verifier's clock.
#: Issuer and verifier are the same process today, so this only matters if they
#: are ever split across hosts with drifting clocks.
_IAT_LEEWAY_SECONDS: Final = 60


class JwtTokenService(ITokenService):
    """Signs and validates HMAC-signed JWT access tokens."""

    def __init__(
        self,
        *,
        secret: str,
        issuer: str,
        audience: str,
        ttl_seconds: int,
        clock: IClock,
        algorithm: str = "HS256",
    ) -> None:
        if algorithm not in _SUPPORTED_ALGORITHMS:
            raise ConfigurationError(f"unsupported JWT algorithm: {algorithm!r}")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigurationError(
                f"JWT signing secret must be at least {MIN_SECRET_LENGTH} characters"
            )
        if ttl_seconds <= 0:
            raise ConfigurationError("JWT token lifetime must be positive")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl_seconds
        self._clock = clock
        self._algorithm = algorithm

    def issue(self, *, subject: str, role: str) -> IssuedToken:
        issued_at = self._clock.now()
        expires_at = issued_at + timedelta(seconds=self._ttl)
        payload: dict[str, Any] = {
            "sub": subject,
            CLAIM_ROLE: role,
            CLAIM_TOKEN_TYPE: ACCESS_TOKEN_TYPE,
            "iat": int(issued_at.timestamp()),
            # Whole seconds, matching the claim's resolution, so the expiry a
            # client is told matches the one that will be enforced.
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
            # A unique id per token, so a future revocation list has something to
            # name. Nothing consumes it yet.
            "jti": uuid4().hex,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return IssuedToken(
            access_token=token,
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
            expires_in_seconds=self._ttl,
        )

    def verify(self, token: str) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": _REQUIRED_CLAIMS,
                    # Presence is still required (above); only the library's
                    # comparison against its own "now" is disabled, because it
                    # cannot be told what time this service thinks it is.
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise ExpiredTokenError("access token has expired") from exc
        except jwt.InvalidTokenError as exc:
            # Covers a bad signature, a malformed token, a disallowed algorithm,
            # a wrong issuer or audience, and a missing required claim.
            raise InvalidTokenError("access token is not valid") from exc

        subject = payload["sub"]
        role = payload[CLAIM_ROLE]
        if not isinstance(subject, str) or not isinstance(role, str):
            raise InvalidTokenError("access token claims are malformed")
        if payload[CLAIM_TOKEN_TYPE] != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("token is not an access token")
        if role not in {member.value for member in Role}:
            raise InvalidTokenError("access token names an unknown role")

        issued_at = self._claim_time(payload, "iat")
        expires_at = self._claim_time(payload, "exp")
        now = self._clock.now()
        if expires_at <= now:
            raise ExpiredTokenError("access token has expired")
        if issued_at > now + timedelta(seconds=_IAT_LEEWAY_SECONDS):
            raise InvalidTokenError("access token was issued in the future")

        return TokenClaims(
            subject=subject,
            role=role,
            issued_at=issued_at,
            expires_at=expires_at,
            token_type=ACCESS_TOKEN_TYPE,
        )

    @staticmethod
    def _claim_time(payload: dict[str, Any], claim: str) -> datetime:
        """Read a NumericDate claim as an aware UTC datetime, or reject the token."""
        raw = payload[claim]
        # bool is a subclass of int, so exclude it explicitly: ``True`` would
        # otherwise read as the epoch plus one second.
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise InvalidTokenError(f"access token {claim} claim is not a timestamp")
        try:
            return datetime.fromtimestamp(raw, UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise InvalidTokenError(f"access token {claim} claim is out of range") from exc
