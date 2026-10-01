"""Commands for the auth use cases.

Framework-free input DTOs, as in :mod:`app.application.documents.commands`.

:class:`AuthenticateUserCommand` carries a plaintext password for exactly as long
as it takes to verify it, so its ``__repr__`` is masked. A generated repr would
put the password into any log line, assertion failure, or traceback frame summary
that touched the command.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticateUserCommand:
    """Credentials submitted to the token endpoint.

    ``password`` is a plain ``str``, not a :class:`Password` value object: a login
    attempt must not be checked against the password policy. Rejecting a short
    password with "too short" before checking it would leak the policy and return
    422 where the only correct answer is 401.
    """

    email: str
    password: str

    def __repr__(self) -> str:
        return f"AuthenticateUserCommand(email={self.email!r}, password=***)"
