"""The JWT access-token service (BRD AC-8.1).

Expiry is tested by advancing a :class:`FixedClock`, never by sleeping: the
adapter judges ``exp``/``iat`` against its injected clock rather than the system
clock, so "two hours from now" is a construction parameter and the test runs in
microseconds.

Several tests mint tokens with PyJWT directly. That is the point -- they are
forgeries an attacker could construct, and the assertion is that the adapter
refuses them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from app.application.errors import (
    ConfigurationError,
    ExpiredTokenError,
    InvalidTokenError,
)
from app.application.ports.tokens import ACCESS_TOKEN_TYPE
from app.infrastructure.auth.token_service import (
    MIN_SECRET_LENGTH,
    JwtTokenService,
)
from tests.support.fakes import FixedClock

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_SECRET = "k" * MIN_SECRET_LENGTH
_ISSUER = "domain-copilot"
_AUDIENCE = "domain-copilot-api"
_TTL = 900
_SUBJECT = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _service(*, at: datetime = _NOW, secret: str = _SECRET, **kwargs: Any) -> JwtTokenService:
    params: dict[str, Any] = {
        "secret": secret,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "ttl_seconds": _TTL,
        "clock": FixedClock(at),
    }
    params.update(kwargs)
    return JwtTokenService(**params)


def _mint(claims: dict[str, Any], *, secret: str = _SECRET, algorithm: str = "HS256") -> str:
    """Hand-build a token, to test what the adapter refuses."""
    payload = {
        "sub": _SUBJECT,
        "role": "analyst",
        "token_type": ACCESS_TOKEN_TYPE,
        "iat": int(_NOW.timestamp()),
        "exp": int((_NOW + timedelta(seconds=_TTL)).timestamp()),
        "iss": _ISSUER,
        "aud": _AUDIENCE,
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not _REMOVE}
    return pyjwt.encode(payload, secret, algorithm=algorithm)


_REMOVE = object()


def _decode_claims(token: str) -> dict[str, Any]:
    """Read a token's claims for inspection, ignoring their time validity.

    PyJWT checks ``exp``/``iat`` against the *real* system clock and cannot be told
    otherwise, so a test that pins a fixed instant would start failing once wall
    time passed it. These assertions are about which claims are present, and the
    adapter's own time handling is covered by the fixed-clock tests below.
    """
    decoded: dict[str, Any] = pyjwt.decode(
        token,
        _SECRET,
        algorithms=["HS256"],
        issuer=_ISSUER,
        audience=_AUDIENCE,
        options={"verify_exp": False, "verify_iat": False},
    )
    return decoded


@pytest.fixture
def service() -> JwtTokenService:
    return _service()


@pytest.fixture
def token(service: JwtTokenService) -> str:
    return service.issue(subject=_SUBJECT, role="analyst").access_token


class TestConfiguration:
    def test_refuses_a_short_signing_secret(self) -> None:
        with pytest.raises(ConfigurationError):
            _service(secret="too-short")

    def test_refuses_an_empty_signing_secret(self) -> None:
        with pytest.raises(ConfigurationError):
            _service(secret="")

    @pytest.mark.parametrize("algorithm", ["none", "None", "RS256", "ES256", "HS1", "made-up"])
    def test_refuses_an_unsupported_algorithm(self, algorithm: str) -> None:
        # Restricting the allow-list at construction is the first half of the
        # algorithm-confusion defence; pinning it on decode is the second.
        with pytest.raises(ConfigurationError):
            _service(algorithm=algorithm)

    @pytest.mark.parametrize(
        ("algorithm", "key_bytes"), [("HS256", 32), ("HS384", 48), ("HS512", 64)]
    )
    def test_accepts_the_supported_hmac_algorithms(self, algorithm: str, key_bytes: int) -> None:
        svc = _service(algorithm=algorithm, secret="k" * key_bytes)
        assert svc.verify(svc.issue(subject=_SUBJECT, role="analyst").access_token)

    @pytest.mark.parametrize(
        ("algorithm", "key_bytes"), [("HS384", 47), ("HS512", 63), ("HS256", 31)]
    )
    def test_refuses_a_secret_below_the_algorithms_key_length(
        self, algorithm: str, key_bytes: int
    ) -> None:
        # RFC 7518 §3.2 requires a key at least as long as the hash output, so the
        # floor rises with the algorithm rather than staying at HS256's 32 bytes.
        with pytest.raises(ConfigurationError):
            _service(algorithm=algorithm, secret="k" * key_bytes)

    def test_secret_length_is_measured_in_bytes(self) -> None:
        # 31 two-byte characters is 62 bytes: long enough as a string, short as a
        # key. Measuring characters would let this through.
        assert len("é" * 31) < MIN_SECRET_LENGTH
        _service(secret="é" * 16)  # 32 bytes: accepted
        with pytest.raises(ConfigurationError):
            _service(secret="é" * 15)  # 30 bytes: refused

    @pytest.mark.parametrize("ttl", [0, -1, -900])
    def test_refuses_a_non_positive_lifetime(self, ttl: int) -> None:
        with pytest.raises(ConfigurationError):
            _service(ttl_seconds=ttl)


class TestIssuing:
    def test_issues_a_three_part_jwt(self, token: str) -> None:
        assert token.count(".") == 2

    def test_reports_the_configured_lifetime(self, service: JwtTokenService) -> None:
        issued = service.issue(subject=_SUBJECT, role="analyst")
        assert issued.expires_in_seconds == _TTL
        assert issued.expires_at == _NOW + timedelta(seconds=_TTL)
        assert issued.token_type == "bearer"

    def test_the_issued_token_repr_is_masked(self, service: JwtTokenService) -> None:
        issued = service.issue(subject=_SUBJECT, role="analyst")
        assert issued.access_token not in repr(issued)
        assert "***" in repr(issued)

    def test_each_token_is_unique(self, service: JwtTokenService) -> None:
        # A distinct jti per token, so a future revocation list can name one.
        first = service.issue(subject=_SUBJECT, role="analyst").access_token
        second = service.issue(subject=_SUBJECT, role="analyst").access_token
        assert first != second

    def test_carries_the_expected_claims(self, token: str) -> None:
        decoded = _decode_claims(token)
        assert decoded["sub"] == _SUBJECT
        assert decoded["role"] == "analyst"
        assert decoded["token_type"] == ACCESS_TOKEN_TYPE
        assert decoded["iss"] == _ISSUER
        assert decoded["aud"] == _AUDIENCE
        assert decoded["iat"] == int(_NOW.timestamp())
        assert decoded["exp"] == int((_NOW + timedelta(seconds=_TTL)).timestamp())
        assert "jti" in decoded

    def test_carries_no_credential(self, token: str) -> None:
        # A JWT payload is only base64-encoded, not encrypted. Nothing secret may
        # go in it.
        decoded = _decode_claims(token)
        assert "password" not in decoded
        assert "hashed_password" not in decoded
        assert not any("hash" in key for key in decoded)


class TestVerifyingAValidToken:
    def test_returns_the_claims(self, service: JwtTokenService, token: str) -> None:
        claims = service.verify(token)
        assert claims.subject == _SUBJECT
        assert claims.role == "analyst"
        assert claims.token_type == ACCESS_TOKEN_TYPE
        assert claims.issued_at == _NOW
        assert claims.expires_at == _NOW + timedelta(seconds=_TTL)


class TestExpiry:
    def test_a_token_one_second_before_expiry_is_accepted(self, token: str) -> None:
        late = _service(at=_NOW + timedelta(seconds=_TTL - 1))
        assert late.verify(token).subject == _SUBJECT

    def test_a_token_at_its_expiry_instant_is_rejected(self, token: str) -> None:
        # exp is inclusive of rejection: valid strictly before it.
        late = _service(at=_NOW + timedelta(seconds=_TTL))
        with pytest.raises(ExpiredTokenError):
            late.verify(token)

    def test_a_long_expired_token_is_rejected(self, token: str) -> None:
        late = _service(at=_NOW + timedelta(days=30))
        with pytest.raises(ExpiredTokenError):
            late.verify(token)

    def test_expiry_is_judged_by_the_injected_clock(self, token: str) -> None:
        # The same token, two verifiers, different clocks. If the adapter used the
        # real system clock this could not be asserted deterministically.
        assert _service(at=_NOW).verify(token)
        with pytest.raises(ExpiredTokenError):
            _service(at=_NOW + timedelta(hours=1)).verify(token)

    def test_a_token_issued_in_the_future_is_rejected(self, service: JwtTokenService) -> None:
        forged = _mint({"iat": int((_NOW + timedelta(days=1)).timestamp())})
        with pytest.raises(InvalidTokenError):
            service.verify(forged)

    def test_small_clock_skew_on_iat_is_tolerated(self, service: JwtTokenService) -> None:
        # Issuer and verifier may sit on hosts a few seconds apart.
        skewed = _mint({"iat": int((_NOW + timedelta(seconds=5)).timestamp())})
        assert service.verify(skewed).subject == _SUBJECT


class TestRejectingBadTokens:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "not-a-jwt",
            "a.b",
            "a.b.c",
            "....",
            "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x",
        ],
    )
    def test_a_malformed_token_is_rejected(self, service: JwtTokenService, bad: str) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(bad)

    def test_a_tampered_signature_is_rejected(self, service: JwtTokenService, token: str) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(token[:-4] + "AAAA")

    def test_a_tampered_payload_is_rejected(self, service: JwtTokenService, token: str) -> None:
        # Swap in an admin payload while keeping the original signature.
        header, _, signature = token.split(".")
        forged_payload = _mint({"role": "admin"}).split(".")[1]
        with pytest.raises(InvalidTokenError):
            service.verify(f"{header}.{forged_payload}.{signature}")

    def test_a_token_signed_with_another_secret_is_rejected(self, service: JwtTokenService) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({}, secret="z" * MIN_SECRET_LENGTH))

    def test_an_unsigned_token_is_rejected(self, service: JwtTokenService) -> None:
        # The classic alg=none forgery. Pinning algorithms on decode is what stops
        # this; without it the token below would authenticate as an admin.
        forged = _mint({"role": "admin"}, secret="", algorithm="none")
        with pytest.raises(InvalidTokenError):
            service.verify(forged)

    def test_a_token_for_another_issuer_is_rejected(self, service: JwtTokenService) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"iss": "some-other-service"}))

    def test_a_token_for_another_audience_is_rejected(self, service: JwtTokenService) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"aud": "some-other-api"}))

    @pytest.mark.parametrize("claim", ["sub", "role", "token_type", "iat", "exp", "iss", "aud"])
    def test_a_missing_required_claim_is_rejected(
        self, service: JwtTokenService, claim: str
    ) -> None:
        # A correctly signed token is still refused if a claim the authorization
        # decision depends on is absent, rather than being defaulted.
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({claim: _REMOVE}))

    def test_a_refresh_token_cannot_be_replayed_as_an_access_token(
        self, service: JwtTokenService
    ) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"token_type": "refresh"}))

    @pytest.mark.parametrize("role", ["superuser", "staff", "manager", "moderator", "ADMIN", ""])
    def test_a_token_naming_an_unknown_role_is_rejected(
        self, service: JwtTokenService, role: str
    ) -> None:
        # A signed token asserting a role this system does not define must not be
        # carried into an authorization decision.
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"role": role}))

    @pytest.mark.parametrize("subject", [123, None, True, ["a"], {"a": 1}])
    def test_a_non_string_subject_is_rejected(
        self, service: JwtTokenService, subject: object
    ) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"sub": subject}))

    @pytest.mark.parametrize("claim", ["iat", "exp"])
    @pytest.mark.parametrize("value", [True, False, "1700000000", None, [1]])
    def test_a_non_numeric_time_claim_is_rejected(
        self, service: JwtTokenService, claim: str, value: object
    ) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({claim: value}))

    @pytest.mark.parametrize("value", [10**18, -(10**18)])
    def test_an_out_of_range_time_claim_is_rejected(
        self, service: JwtTokenService, value: int
    ) -> None:
        with pytest.raises(InvalidTokenError):
            service.verify(_mint({"exp": value}))


class TestErrorSafety:
    def test_errors_never_contain_the_token(self, service: JwtTokenService) -> None:
        # "Do not log raw JWTs" has to hold for error messages too, since those
        # are what gets logged.
        forged = _mint({}, secret="z" * MIN_SECRET_LENGTH)
        with pytest.raises(InvalidTokenError) as caught:
            service.verify(forged)
        assert forged not in str(caught.value)
        assert forged.split(".")[1] not in str(caught.value)

    def test_errors_never_contain_the_signing_secret(self, service: JwtTokenService) -> None:
        with pytest.raises(InvalidTokenError) as caught:
            service.verify("not-a-jwt")
        assert _SECRET not in str(caught.value)

    def test_errors_do_not_echo_the_library_message(self, service: JwtTokenService) -> None:
        # The library's text is kept as __cause__ for a server-side traceback, but
        # the message the boundary sees is ours.
        with pytest.raises(InvalidTokenError) as caught:
            service.verify("not-a-jwt")
        assert str(caught.value) == "access token is not valid"
        assert caught.value.__cause__ is not None
