"""The bcrypt password hasher (BRD AC-8.1).

Exercises the real algorithm, so these are the slowest tests in the unit suite --
deliberately, since the point of a password hash is to be slow. ``rounds`` is set
to the adapter's minimum throughout to keep the cost bounded while still going
through genuine bcrypt.
"""

from __future__ import annotations

import pytest

from app.domain.auth.value_objects import PASSWORD_MAX_BYTES, Password
from app.infrastructure.auth.password_hasher import (
    DEFAULT_BCRYPT_ROUNDS,
    BcryptPasswordHasher,
)

_PASSWORD = "correct horse battery staple"
_ROUNDS = 10


@pytest.fixture(scope="module")
def hasher() -> BcryptPasswordHasher:
    return BcryptPasswordHasher(rounds=_ROUNDS)


@pytest.fixture(scope="module")
def digest(hasher: BcryptPasswordHasher) -> str:
    return hasher.hash(Password(_PASSWORD))


class TestConfiguration:
    def test_default_rounds_meet_the_owasp_floor(self) -> None:
        assert DEFAULT_BCRYPT_ROUNDS >= 12

    @pytest.mark.parametrize("rounds", [0, 4, 9, 17, 31, -1])
    def test_refuses_a_work_factor_outside_the_supported_range(self, rounds: int) -> None:
        # A too-low factor is a security regression; a too-high one would make
        # login unusable. Both are configuration mistakes, caught at construction.
        with pytest.raises(ValueError):
            BcryptPasswordHasher(rounds=rounds)


class TestHashing:
    def test_the_digest_is_not_the_password(self, digest: str) -> None:
        assert digest != _PASSWORD
        assert _PASSWORD not in digest

    def test_produces_a_recognisable_bcrypt_digest(self, digest: str) -> None:
        assert digest.startswith("$2b$")
        assert len(digest) == 60

    def test_the_digest_records_its_own_work_factor(self, digest: str) -> None:
        # This is why raising `rounds` later does not invalidate stored hashes.
        assert digest.startswith(f"$2b${_ROUNDS:02d}$")

    def test_the_same_password_hashes_differently_each_time(
        self, hasher: BcryptPasswordHasher
    ) -> None:
        # Per-digest salt: two users with the same password have different rows,
        # and a precomputed table is useless.
        assert hasher.hash(Password(_PASSWORD)) != hasher.hash(Password(_PASSWORD))

    def test_a_password_at_the_byte_limit_still_hashes(self, hasher: BcryptPasswordHasher) -> None:
        at_limit = "x" * PASSWORD_MAX_BYTES
        assert hasher.verify(at_limit, hasher.hash(Password(at_limit)))


class TestVerification:
    def test_the_correct_password_verifies(self, hasher: BcryptPasswordHasher, digest: str) -> None:
        assert hasher.verify(_PASSWORD, digest)

    @pytest.mark.parametrize(
        "wrong",
        [
            "correct horse battery stapl",  # one character short
            "correct horse battery staple ",  # trailing space
            "Correct horse battery staple",  # different case
            "",
            "totally unrelated",
        ],
    )
    def test_a_wrong_password_does_not_verify(
        self, hasher: BcryptPasswordHasher, digest: str, wrong: str
    ) -> None:
        assert not hasher.verify(wrong, digest)

    def test_a_digest_from_another_password_does_not_verify(
        self, hasher: BcryptPasswordHasher
    ) -> None:
        other = hasher.hash(Password("a-different-password-entirely"))
        assert not hasher.verify(_PASSWORD, other)

    def test_an_independently_created_digest_of_the_same_password_verifies(
        self, hasher: BcryptPasswordHasher
    ) -> None:
        # Verification depends on the password, not on which call produced the
        # digest -- so a row written by the seeder verifies at login.
        assert hasher.verify(_PASSWORD, hasher.hash(Password(_PASSWORD)))


class TestHostileInput:
    def test_an_overlong_password_is_rejected_without_raising(
        self, hasher: BcryptPasswordHasher, digest: str
    ) -> None:
        # bcrypt 5.x raises above 72 bytes instead of truncating. On the login
        # path that would be a 500 on an unauthenticated endpoint, so the adapter
        # measures first and answers False.
        assert not hasher.verify("x" * (PASSWORD_MAX_BYTES + 1), digest)
        assert not hasher.verify("x" * 10_000, digest)

    def test_a_multibyte_password_over_the_limit_is_rejected(
        self, hasher: BcryptPasswordHasher, digest: str
    ) -> None:
        # 40 of these are 80 UTF-8 bytes: the limit is on bytes, not characters.
        assert not hasher.verify("é" * 40, digest)

    @pytest.mark.parametrize(
        "bad_digest",
        [
            "",
            "not-a-bcrypt-hash",
            "$2b$",
            "$2b$10$too-short",
            "plaintext-password-stored-by-mistake",
            "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aaaa",
        ],
    )
    def test_a_corrupt_or_foreign_digest_does_not_raise(
        self, hasher: BcryptPasswordHasher, bad_digest: str
    ) -> None:
        # A bad row in the store must fail the login, not crash the request.
        assert not hasher.verify(_PASSWORD, bad_digest)


class TestDummyVerify:
    def test_does_not_raise(self, hasher: BcryptPasswordHasher) -> None:
        # The whole contract: it does the work and discards the result.
        hasher.dummy_verify("any-password")

    def test_is_safe_to_call_repeatedly(self, hasher: BcryptPasswordHasher) -> None:
        for _ in range(3):
            hasher.dummy_verify("any-password")

    def test_absorbs_an_overlong_password(self, hasher: BcryptPasswordHasher) -> None:
        # The unknown-account path receives whatever the client sent.
        hasher.dummy_verify("x" * 10_000)

    def test_its_throwaway_digest_matches_no_real_password(self) -> None:
        # Built from random bytes, so it cannot accidentally accept anything.
        hasher = BcryptPasswordHasher(rounds=_ROUNDS)
        hasher.dummy_verify("probe")
        internal = hasher._dummy_hash  # noqa: SLF001 - asserting an internal invariant
        assert internal is not None
        assert not hasher.verify("probe", internal)
        assert not hasher.verify("", internal)
