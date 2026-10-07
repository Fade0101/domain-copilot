"""Password hashing port (BRD AC-8.1).

The application layer decides *when* a password is hashed or verified; it never
knows *how*. bcrypt/Argon2 live behind this port in
:mod:`app.infrastructure.auth`, so no hashing library is importable from domain
or application code.

Note the asymmetry between the two verbs, which is deliberate:

* :meth:`hash` takes a :class:`Password` value object, because setting a
  credential must satisfy the password policy.
* :meth:`verify` takes a plain ``str``, because a *login attempt* must not be
  validated against the policy first -- doing so would turn "your password is
  too short to be correct" into an oracle and would return 422 where the only
  correct answer is 401.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.auth.value_objects import Password


class IPasswordHasher(Protocol):
    """Hashes and verifies passwords with a deliberately slow, salted algorithm."""

    def hash(self, password: Password) -> str:
        """Return a salted digest of ``password``, suitable for ``users.hashed_password``.

        Two calls with the same password return different digests (unique salt).
        """
        ...

    def verify(self, plaintext: str, hashed_password: str) -> bool:
        """Return whether ``plaintext`` matches ``hashed_password``.

        Must return ``False`` -- never raise -- for any input the algorithm cannot
        process (an over-long password, a corrupt or foreign digest). An exception
        here would surface as a 500 on an unauthenticated endpoint.
        """
        ...

    def dummy_verify(self, plaintext: str) -> None:
        """Do the work of a verification and discard the result.

        Called when no user matches the submitted email so that a login for an
        unknown account costs roughly the same wall-clock time as one for a known
        account, denying an attacker a timing oracle for enumerating users.
        """
        ...
