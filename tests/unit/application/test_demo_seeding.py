"""Demo-account seeding (FR-8 §10).

The properties that matter are that the exact three accounts and roles are
created, that running it again changes nothing, and that no plaintext password is
stored anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.auth.seeding import (
    DEMO_ACCOUNTS,
    DemoAccountSpec,
    SeedDemoAccountsUseCase,
)
from app.domain.auth.value_objects import EmailAddress, Password, Role
from app.domain.shared.errors import InvariantViolationError
from tests.support.fakes import (
    FakePasswordHasher,
    FakeUserRepository,
    FixedClock,
    SequentialIdGenerator,
)

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_PASSWORD = "dev-only-password-1234"


@pytest.fixture
def users() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def use_case(users: FakeUserRepository) -> SeedDemoAccountsUseCase:
    return SeedDemoAccountsUseCase(
        users=users,
        password_hasher=FakePasswordHasher(),
        clock=FixedClock(_NOW),
        id_generator=SequentialIdGenerator(),
    )


class TestDemoAccountSpecs:
    def test_there_is_exactly_one_account_per_role(self) -> None:
        roles = [spec.role for spec in DEMO_ACCOUNTS]
        assert sorted(role.value for role in roles) == ["admin", "analyst", "reviewer"]
        assert len(set(roles)) == len(Role)

    def test_addresses_use_the_reserved_example_domain(self) -> None:
        # RFC 2606: example.com cannot reach a real mailbox, so a stray demo
        # account cannot email anyone.
        for spec in DEMO_ACCOUNTS:
            assert spec.email.endswith("@example.com")

    def test_no_password_is_present_in_the_specs(self) -> None:
        # The credential comes from the environment; nothing here carries one.
        for spec in DEMO_ACCOUNTS:
            assert not hasattr(spec, "password")


class TestSeeding:
    async def test_creates_the_three_accounts_with_the_right_roles(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        result = await use_case.execute(Password(_PASSWORD))
        assert len(result.created) == 3
        assert result.skipped == ()

        by_email = {user.email.value: user for user in users.users.values()}
        assert by_email["analyst@example.com"].role is Role.ANALYST
        assert by_email["reviewer@example.com"].role is Role.REVIEWER
        assert by_email["admin@example.com"].role is Role.ADMIN

    async def test_no_account_stores_the_plaintext_password(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        await use_case.execute(Password(_PASSWORD))
        for user in users.users.values():
            assert user.hashed_password != _PASSWORD
            assert _PASSWORD not in repr(user)

    async def test_running_it_again_creates_nothing(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        first = await use_case.execute(Password(_PASSWORD))
        snapshot = {uid: user for uid, user in users.users.items()}

        second = await use_case.execute(Password(_PASSWORD))
        assert first.created == tuple(sorted(first.created, key=first.created.index))
        assert second.created == ()
        assert len(second.skipped) == 3
        # Same rows, same ids, same digests: nothing was recreated or rewritten.
        assert users.users == snapshot

    async def test_a_changed_password_is_not_overwritten_by_reseeding(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        await use_case.execute(Password(_PASSWORD))
        analyst = await users.get_by_email(EmailAddress("analyst@example.com"))
        assert analyst is not None
        original_digest = analyst.hashed_password

        await use_case.execute(Password("a-different-password-9876"))
        reloaded = await users.get_by_email(EmailAddress("analyst@example.com"))
        assert reloaded is not None
        assert reloaded.hashed_password == original_digest

    async def test_each_account_gets_its_own_hash_invocation(
        self, users: FakeUserRepository
    ) -> None:
        # Real bcrypt salts per call, so three accounts sharing a password still
        # get three distinct digests. Asserted through the port so this holds for
        # whichever hasher is wired in.
        hashed: list[str] = []

        class CountingHasher(FakePasswordHasher):
            def hash(self, password: Password) -> str:
                digest = f"{super().hash(password)}::{len(hashed)}"
                hashed.append(digest)
                return digest

        use_case = SeedDemoAccountsUseCase(
            users=users,
            password_hasher=CountingHasher(),
            clock=FixedClock(_NOW),
            id_generator=SequentialIdGenerator(),
        )
        await use_case.execute(Password(_PASSWORD))
        assert len(hashed) == 3
        assert len({user.hashed_password for user in users.users.values()}) == 3

    async def test_created_at_comes_from_the_clock(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        await use_case.execute(Password(_PASSWORD))
        assert all(user.created_at == _NOW for user in users.users.values())

    async def test_a_weak_demo_password_is_refused(self) -> None:
        # The password arrives as a Password value object, so a demo password that
        # fails the policy is rejected rather than quietly creating weak accounts.
        with pytest.raises(InvariantViolationError):
            Password("short")

    async def test_accepts_an_explicit_account_list(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        result = await use_case.execute(
            Password(_PASSWORD),
            accounts=(DemoAccountSpec(email="solo@example.com", role=Role.ADMIN),),
        )
        assert result.created == ("solo@example.com",)
        assert len(users.users) == 1

    async def test_email_is_normalized_before_the_uniqueness_check(
        self, use_case: SeedDemoAccountsUseCase, users: FakeUserRepository
    ) -> None:
        await use_case.execute(
            Password(_PASSWORD),
            accounts=(DemoAccountSpec(email="Mixed@Example.COM", role=Role.ANALYST),),
        )
        second = await use_case.execute(
            Password(_PASSWORD),
            accounts=(DemoAccountSpec(email="mixed@example.com", role=Role.ANALYST),),
        )
        assert second.created == ()
        assert len(users.users) == 1

    async def test_result_reports_totals(self, use_case: SeedDemoAccountsUseCase) -> None:
        assert (await use_case.execute(Password(_PASSWORD))).total == 3
        assert (await use_case.execute(Password(_PASSWORD))).total == 3

    async def test_result_never_carries_the_password(
        self, use_case: SeedDemoAccountsUseCase
    ) -> None:
        result = await use_case.execute(Password(_PASSWORD))
        assert _PASSWORD not in repr(result)
