"""Auth domain value objects and the User entity (BRD FR-8).

Covers the guarantees the rest of the auth stack is allowed to assume: that a
``UserId`` is a real UUID, that an ``EmailAddress`` is normalized before it ever
reaches a uniqueness check, that a ``Password`` satisfies the policy and cannot
exceed what the hasher accepts, and that neither a password nor a hash can be
printed by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.auth.entities import User
from app.domain.auth.value_objects import (
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_LENGTH,
    EmailAddress,
    Password,
    Permission,
    ResourceType,
    Role,
    UserId,
)
from app.domain.shared.errors import InvariantViolationError


class TestRole:
    def test_exactly_three_roles_exist(self) -> None:
        # BRD AC-8.2 defines three roles. No superuser, staff, manager, or
        # moderator: a fourth member here would be a privilege tier nothing in
        # the permission matrix accounts for.
        assert [role.value for role in Role] == ["analyst", "reviewer", "admin"]

    def test_role_is_its_own_string_value(self) -> None:
        assert Role.ADMIN == "admin"
        assert f"{Role.ANALYST}" == "analyst"

    def test_unknown_role_string_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Role("superuser")


class TestUserId:
    def test_accepts_a_uuid(self) -> None:
        value = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert UserId(value).value == value

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-uuid", "12345", "3f2504e0-4f89"])
    def test_rejects_anything_that_is_not_a_uuid(self, bad: str) -> None:
        with pytest.raises(InvariantViolationError):
            UserId(bad)

    def test_is_immutable(self) -> None:
        user_id = UserId("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
        with pytest.raises(Exception):
            user_id.value = "other"  # type: ignore[misc]

    def test_equal_values_compare_equal(self) -> None:
        value = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert UserId(value) == UserId(value)


class TestEmailAddress:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Analyst@Example.COM", "analyst@example.com"),
            ("  analyst@example.com  ", "analyst@example.com"),
            ("\tADMIN@EXAMPLE.COM\n", "admin@example.com"),
        ],
    )
    def test_is_normalized(self, raw: str, expected: str) -> None:
        # Normalization is what stops "Admin@example.com" from registering a
        # second account alongside "admin@example.com" despite the unique index.
        assert EmailAddress(raw).value == expected

    @pytest.mark.parametrize(
        "bad", ["", "   ", "no-at-sign", "@example.com", "user@", "user@host", "a b@c.com"]
    )
    def test_rejects_malformed_addresses(self, bad: str) -> None:
        with pytest.raises(InvariantViolationError):
            EmailAddress(bad)

    def test_rejects_an_address_longer_than_the_column(self) -> None:
        # users.email is VARCHAR(255); a longer value would be truncated or error
        # at the database rather than here.
        too_long = "a" * 250 + "@example.com"
        with pytest.raises(InvariantViolationError):
            EmailAddress(too_long)


class TestPassword:
    def test_accepts_a_policy_compliant_password(self) -> None:
        assert Password("correct horse battery").value == "correct horse battery"

    def test_rejects_a_password_below_the_minimum_length(self) -> None:
        with pytest.raises(InvariantViolationError):
            Password("a" * (PASSWORD_MIN_LENGTH - 1))

    def test_rejects_a_password_over_the_hasher_input_limit(self) -> None:
        # bcrypt raises above 72 bytes rather than truncating, so the boundary is
        # enforced here where it can be reported as a policy violation.
        with pytest.raises(InvariantViolationError):
            Password("a" * (PASSWORD_MAX_BYTES + 1))

    def test_length_limit_counts_bytes_not_characters(self) -> None:
        # Multi-byte characters: 30 of these are 90 UTF-8 bytes, over the limit,
        # even though the string is well under 72 characters.
        multibyte = "é" * 40
        assert len(multibyte) < PASSWORD_MAX_BYTES
        assert len(multibyte.encode("utf-8")) > PASSWORD_MAX_BYTES
        with pytest.raises(InvariantViolationError):
            Password(multibyte)

    def test_repr_does_not_contain_the_password(self) -> None:
        secret = "unmistakable-secret-value"
        password = Password(secret)
        assert secret not in repr(password)
        assert secret not in str(password)
        assert secret not in f"{password}"

    def test_repr_of_a_containing_structure_is_also_masked(self) -> None:
        # The realistic leak path: a password held in a tuple/dict that something
        # else logs. dataclass repr is what would expose it.
        secret = "unmistakable-secret-value"
        assert secret not in repr({"credentials": Password(secret)})


class TestResourceType:
    def test_covers_every_ownership_checked_resource(self) -> None:
        values = {member.value for member in ResourceType}
        assert {"run", "job", "trace", "session"} <= values


class TestUser:
    def _user(self, role: Role = Role.ANALYST) -> User:
        return User(
            id=UserId("3f2504e0-4f89-41d3-9a0c-0305e82c3301"),
            email=EmailAddress("analyst@example.com"),
            role=role,
            hashed_password="$2b$12$abcdefghijklmnopqrstuv",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
    def test_rejects_an_empty_password_hash(self, bad: str) -> None:
        # A blank hash would make every verification fail closed, but it would
        # also mean a user row exists with no credential at all.
        with pytest.raises(InvariantViolationError):
            User(
                id=UserId("3f2504e0-4f89-41d3-9a0c-0305e82c3301"),
                email=EmailAddress("analyst@example.com"),
                role=Role.ANALYST,
                hashed_password=bad,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_repr_masks_the_password_hash(self) -> None:
        user = self._user()
        assert "$2b$12$abcdefghijklmnopqrstuv" not in repr(user)
        assert "hashed_password=***" in repr(user)
        # The email and role stay visible: they are what makes a log line useful.
        assert "analyst@example.com" in repr(user)

    def test_has_permission_follows_the_role_matrix(self) -> None:
        assert self._user(Role.ANALYST).has_permission(Permission.RUN_WORKFLOW)
        assert not self._user(Role.ANALYST).has_permission(Permission.MANAGE_USERS)
        assert self._user(Role.ADMIN).has_permission(Permission.MANAGE_USERS)

    def test_owns_is_true_only_for_its_own_id(self) -> None:
        user = self._user()
        assert user.owns(UserId("3f2504e0-4f89-41d3-9a0c-0305e82c3301"))
        assert not user.owns(UserId("11111111-1111-4111-8111-111111111111"))

    def test_nobody_owns_an_unowned_object(self) -> None:
        # An object with no recorded owner must not become readable by everyone.
        assert not self._user(Role.ANALYST).owns(None)
        assert not self._user(Role.ADMIN).owns(None)

    def test_is_immutable(self) -> None:
        user = self._user()
        with pytest.raises(Exception):
            user.role = Role.ADMIN  # type: ignore[misc]
