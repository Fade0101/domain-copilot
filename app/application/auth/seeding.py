"""Demo-account seeding (Ticket #5 development fixtures).

Creates one account per role so the RBAC matrix can be exercised end to end
without a registration endpoint. **Development and demo only** -- the composition
root refuses to run this when ``ENVIRONMENT=production``.

Three properties this has to have, and how each is obtained:

* **No committed credential.** The password is not in this file, or anywhere in
  the repository. It arrives as ``AUTH__DEMO_PASSWORD`` from the environment, and
  when it is absent seeding is skipped with a warning rather than falling back to
  something guessable.
* **Idempotent.** Existing accounts are detected by email and left untouched, so
  running this twice creates nothing the second time and never overwrites a
  password that has been changed.
* **Deterministic.** The same three emails and the same three roles, every run.
  Only the generated ids differ between fresh databases, which is immaterial --
  idempotency is keyed on email, matching the ``ix_users_email`` unique index.

The password is taken as a :class:`Password` value object, so a demo password
that fails the policy is rejected here instead of producing accounts nobody
expected to be weak.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.ports.passwords import IPasswordHasher
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock, IIdGenerator
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, Password, Role, UserId


@dataclass(frozen=True, slots=True)
class DemoAccountSpec:
    """One demo account: the login email and the role it exercises."""

    email: str
    role: Role


#: The demo accounts, one per role in the AC-8.2 matrix. ``example.com`` is the
#: RFC 2606 reserved domain, so none of these addresses can reach a real mailbox.
DEMO_ACCOUNTS: tuple[DemoAccountSpec, ...] = (
    DemoAccountSpec(email="analyst@example.com", role=Role.ANALYST),
    DemoAccountSpec(email="reviewer@example.com", role=Role.REVIEWER),
    DemoAccountSpec(email="admin@example.com", role=Role.ADMIN),
)


@dataclass(frozen=True, slots=True)
class SeedDemoAccountsResult:
    """What a seeding run did. Emails only -- never the password that was set."""

    created: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.created) + len(self.skipped)


class SeedDemoAccountsUseCase:
    """Create the per-role demo accounts if they are not already present."""

    def __init__(
        self,
        users: IUserRepository,
        password_hasher: IPasswordHasher,
        clock: IClock,
        id_generator: IIdGenerator,
    ) -> None:
        self._users = users
        self._hasher = password_hasher
        self._clock = clock
        self._id_generator = id_generator

    async def execute(
        self,
        password: Password,
        accounts: tuple[DemoAccountSpec, ...] = DEMO_ACCOUNTS,
    ) -> SeedDemoAccountsResult:
        created: list[str] = []
        skipped: list[str] = []

        for spec in accounts:
            email = EmailAddress(spec.email)
            if await self._users.get_by_email(email) is not None:
                skipped.append(email.value)
                continue

            # Hashed per account, so each row gets its own salt and the digests
            # do not reveal that the three accounts share a password.
            await self._users.add(
                User(
                    id=UserId(self._id_generator.new_id()),
                    email=email,
                    role=spec.role,
                    hashed_password=self._hasher.hash(password),
                    created_at=self._clock.now(),
                )
            )
            created.append(email.value)

        return SeedDemoAccountsResult(created=tuple(created), skipped=tuple(skipped))
