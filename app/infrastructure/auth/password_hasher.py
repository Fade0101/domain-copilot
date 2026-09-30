"""bcrypt password hasher (implements :class:`IPasswordHasher`; BRD AC-8.1).

The only module in the codebase that imports a hashing library. Domain and
application code reaches it exclusively through the port, which is asserted by
the import-linter contracts and by ``tests/architecture/test_boundaries.py``.

Two bcrypt properties shape this adapter:

* **bcrypt rejects inputs over 72 bytes** rather than truncating them (it has
  done so since 5.0). Truncation would silently make a long password's tail
  irrelevant; raising is correct, but an exception on the *login* path would turn
  an over-long submission into an unauthenticated 500. So :meth:`verify` measures
  first and answers ``False``, which is also the truthful answer -- no stored
  digest can correspond to a password this module refused to hash.
* **A digest carries its own cost parameter.** Raising ``rounds`` later does not
  invalidate existing hashes; they keep verifying at the cost they were made
  with.

``passlib`` is deliberately not used. Its 1.7.4 bcrypt backend probe
(``detect_wrap_bug``) calls ``hashpw`` with a 73-byte input, which bcrypt 5.x
raises on, so merely constructing a ``CryptContext(schemes=["bcrypt"])`` fails.
bcrypt is called directly instead; BRD AC-8.1 names the algorithm, not a wrapper.
"""

from __future__ import annotations

import secrets

import bcrypt

from app.application.ports.passwords import IPasswordHasher
from app.domain.auth.value_objects import PASSWORD_MAX_BYTES, Password

#: OWASP's current floor for bcrypt work factor. Roughly a quarter second per
#: hash on 2025 server hardware -- slow enough to matter to an offline attacker,
#: fast enough for an interactive login.
DEFAULT_BCRYPT_ROUNDS = 12

_MIN_ROUNDS = 10
_MAX_ROUNDS = 16


class BcryptPasswordHasher(IPasswordHasher):
    """Salted bcrypt hashing with a configurable work factor."""

    def __init__(self, rounds: int = DEFAULT_BCRYPT_ROUNDS) -> None:
        if not _MIN_ROUNDS <= rounds <= _MAX_ROUNDS:
            raise ValueError(
                f"bcrypt rounds must be between {_MIN_ROUNDS} and {_MAX_ROUNDS}, got {rounds}"
            )
        self._rounds = rounds
        self._dummy_hash: str | None = None

    def hash(self, password: Password) -> str:
        """Return a ``$2b$``-prefixed digest with a freshly generated salt.

        The :class:`Password` value object has already bounded the input to
        :data:`PASSWORD_MAX_BYTES`, so bcrypt cannot reject it here.
        """
        digest = bcrypt.hashpw(password.value.encode("utf-8"), bcrypt.gensalt(rounds=self._rounds))
        return digest.decode("ascii")

    def verify(self, plaintext: str, hashed_password: str) -> bool:
        """Return whether ``plaintext`` matches ``hashed_password``; never raise.

        ``checkpw`` does the comparison in constant time for a given digest.
        """
        candidate = plaintext.encode("utf-8")
        if len(candidate) > PASSWORD_MAX_BYTES:
            # Unhashable by this adapter, so no stored digest can match it.
            return False
        try:
            return bcrypt.checkpw(candidate, hashed_password.encode("utf-8"))
        except (ValueError, TypeError):
            # A corrupt, empty, or foreign-format digest. Not a match, and not a
            # 500 on an unauthenticated endpoint.
            return False

    def dummy_verify(self, plaintext: str) -> None:
        """Verify ``plaintext`` against a throwaway digest and discard the result.

        Equalises the cost of a login for an unknown account with one for a known
        account. The digest is built lazily -- one bcrypt round-trip on first use
        rather than on every process start -- and is a hash of random bytes, so it
        is neither a secret nor something that could match any real password.
        """
        if self._dummy_hash is None:
            self._dummy_hash = self.hash(Password(secrets.token_urlsafe(32)))
        self.verify(plaintext, self._dummy_hash)
