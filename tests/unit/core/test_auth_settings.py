"""Auth configuration and its fail-closed behaviour (FR-8, constraint C6).

Two things are worth more than the usual settings coverage: that the signing
secret has no usable default, and that the absence of one is handled differently
in development (generate and warn) than in production (refuse to start).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.application.errors import ConfigurationError
from app.core.config import Settings, get_settings
from app.core.container import (
    Container,
    build_database,
    build_jwt_secret,
    get_container,
)
from app.infrastructure.auth.token_service import MIN_SECRET_LENGTH
from app.infrastructure.persistence.database import normalize_database_url
from app.infrastructure.persistence.in_memory.ownership_query import (
    InMemoryOwnershipQuery,
)
from app.infrastructure.persistence.in_memory.user_repository import (
    InMemoryUserRepository,
)
from app.infrastructure.persistence.sql.ownership_query import SqlOwnershipQuery
from app.infrastructure.persistence.sql.user_repository import SqlUserRepository

_AUTH_VARS = (
    "AUTH__SECRET_KEY",
    "AUTH__ALGORITHM",
    "AUTH__ISSUER",
    "AUTH__AUDIENCE",
    "AUTH__ACCESS_TOKEN_TTL_SECONDS",
    "AUTH__BCRYPT_ROUNDS",
    "AUTH__SEED_DEMO_ACCOUNTS",
    "AUTH__DEMO_PASSWORD",
    "DATABASE__URL",
    "DATABASE__ECHO",
)

#: Never connected to -- create_async_engine is lazy, so this only has to parse.
_DUMMY_DB_URL = "postgresql+asyncpg://user:pass@localhost:5432/unused"


def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop inherited auth vars without changing directory.

    The container eagerly loads ``prompts/`` relative to the working directory, so
    tests that build a real :class:`Container` must stay in the project root.
    Explicitly set env vars still take precedence over the developer's ``.env``.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    for name in _AUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    # Keep the real bcrypt work factor at its floor: these tests hash three demo
    # passwords each, and the algorithm itself is covered elsewhere.
    monkeypatch.setenv("AUTH__BCRYPT_ROUNDS", "10")


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make ``Settings()`` hermetic: no developer ``.env``, no inherited auth vars.

    ``chdir`` to an empty directory so the relative ``env_file=".env"`` resolves to
    nothing and the test observes declared defaults. Not usable for tests that
    construct a :class:`Container` -- see :func:`_isolate_env`.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for name in _AUTH_VARS:
        monkeypatch.delenv(name, raising=False)


class TestAuthSettingsDefaults:
    def test_the_signing_secret_has_no_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The central C6 property: a checkout carries no signing key. A default
        # here would be a committed credential shared by every deployment that
        # forgot to override it.
        _isolate(monkeypatch, tmp_path)
        assert Settings().auth.secret_key is None

    def test_the_demo_password_has_no_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        assert Settings().auth.demo_password is None

    def test_remaining_defaults_are_safe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        auth = Settings().auth
        assert auth.algorithm == "HS256"
        assert auth.issuer == "domain-copilot"
        assert auth.audience == "domain-copilot-api"
        assert auth.access_token_ttl_seconds == 3600
        assert auth.bcrypt_rounds >= 12
        assert auth.seed_demo_accounts is True


class TestAuthSettingsBinding:
    def test_nested_env_vars_populate_the_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Uses the existing nested-group mechanism (AUTH__X -> settings.auth.x);
        # no second configuration system was introduced.
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("AUTH__ISSUER", "other-issuer")
        monkeypatch.setenv("AUTH__ACCESS_TOKEN_TTL_SECONDS", "120")
        monkeypatch.setenv("AUTH__BCRYPT_ROUNDS", "11")
        monkeypatch.setenv("AUTH__SEED_DEMO_ACCOUNTS", "false")

        auth = Settings().auth
        assert auth.issuer == "other-issuer"
        assert auth.access_token_ttl_seconds == 120
        assert auth.bcrypt_rounds == 11
        assert auth.seed_demo_accounts is False

    def test_the_secret_is_a_secretstr_and_never_rendered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("AUTH__SECRET_KEY", "an-unmistakable-signing-secret-value")

        auth = Settings().auth
        assert isinstance(auth.secret_key, SecretStr)
        assert auth.secret_key.get_secret_value() == "an-unmistakable-signing-secret-value"
        assert "an-unmistakable-signing-secret-value" not in repr(auth)
        assert "an-unmistakable-signing-secret-value" not in str(auth.secret_key)

    def test_the_demo_password_is_a_secretstr_and_never_rendered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", "an-unmistakable-demo-password")

        auth = Settings().auth
        assert isinstance(auth.demo_password, SecretStr)
        assert "an-unmistakable-demo-password" not in repr(auth)


class TestIsProduction:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("production", True),
            ("PRODUCTION", True),
            ("  Production  ", True),
            ("development", False),
            ("staging", False),
            ("prod", False),
        ],
    )
    def test_recognises_the_production_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str, expected: bool
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("ENVIRONMENT", value)
        assert Settings().is_production() is expected


class TestSigningSecretResolution:
    def test_a_configured_secret_is_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        secret = "x" * MIN_SECRET_LENGTH
        monkeypatch.setenv("AUTH__SECRET_KEY", secret)
        assert build_jwt_secret(Settings()) == secret

    def test_production_refuses_to_start_without_a_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Fail closed. Booting with a secret that rotates on every restart -- or
        # worse, a default one -- is worse than not booting.
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(ConfigurationError, match="AUTH__SECRET_KEY"):
            build_jwt_secret(Settings())

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_production_treats_a_blank_secret_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blank: str
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("AUTH__SECRET_KEY", blank)
        with pytest.raises(ConfigurationError):
            build_jwt_secret(Settings())

    def test_development_generates_an_ephemeral_secret_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger="app.core.container"):
            secret = build_jwt_secret(Settings())

        assert len(secret.encode("utf-8")) >= MIN_SECRET_LENGTH
        assert "AUTH__SECRET_KEY is not configured" in caplog.text

    def test_each_generated_secret_is_different(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Random per process, so nothing predictable signs tokens and tokens do
        # not survive a restart.
        _isolate(monkeypatch, tmp_path)
        settings = Settings()
        assert build_jwt_secret(settings) != build_jwt_secret(settings)

    def test_the_generated_secret_is_not_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        with caplog.at_level(logging.DEBUG):
            secret = build_jwt_secret(Settings())
        assert secret not in caplog.text


class TestDatabaseSelection:
    """Which persistence the composition root picks, and when it refuses to pick one."""

    def test_development_without_a_url_falls_back_to_memory_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _isolate_env(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.core.container"):
            assert build_database(Settings()) is None
        assert "DATABASE__URL is not configured" in caplog.text
        assert "lost on restart" in caplog.text

    def test_a_configured_url_produces_a_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("DATABASE__URL", _DUMMY_DB_URL)
        database = build_database(Settings())
        assert database is not None
        assert database.session_factory is not None

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_url_counts_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("DATABASE__URL", blank)
        assert build_database(Settings()) is None

    def test_in_memory_adapters_are_wired_without_a_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        container = Container(settings=Settings())
        assert isinstance(container.ownership_query, InMemoryOwnershipQuery)
        assert isinstance(container.user_repository, InMemoryUserRepository)
        assert container.database is None

    def test_sql_adapters_are_wired_with_a_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The decisive wiring assertion: with a database configured, ownership is
        # answered by a query against PostgreSQL rather than a process dict.
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        monkeypatch.setenv("DATABASE__URL", _DUMMY_DB_URL)
        container = Container(settings=Settings())
        assert isinstance(container.ownership_query, SqlOwnershipQuery)
        assert isinstance(container.user_repository, SqlUserRepository)
        assert container.database is not None

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (
                "postgresql://u:p@h:5432/db",
                "postgresql+asyncpg://u:p@h:5432/db",
            ),
            (
                "postgres://u:p@h:5432/db",
                "postgresql+asyncpg://u:p@h:5432/db",
            ),
            (
                "postgresql+asyncpg://u:p@h:5432/db",
                "postgresql+asyncpg://u:p@h:5432/db",
            ),
            (
                "  postgresql://u:p@h:5432/db  ",
                "postgresql+asyncpg://u:p@h:5432/db",
            ),
        ],
    )
    def test_a_sync_url_is_rewritten_to_the_async_driver(self, given: str, expected: str) -> None:
        # Every tool and container image hands out postgresql://; pasting one in
        # should not be a startup failure.
        assert normalize_database_url(given) == expected


class TestContainerWiring:
    def test_the_container_exposes_the_auth_collaborators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        container = Container(settings=Settings())

        assert container.user_repository is not None
        assert container.ownership_query is not None
        assert container.password_hasher is not None
        assert container.token_service is not None
        assert container.audit_sink is not None
        assert container.authorization_service is not None

    def test_the_auth_use_cases_are_buildable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        container = Container(settings=Settings())

        assert container.authenticate_user_use_case() is not None
        assert container.resolve_principal_use_case() is not None
        assert container.seed_demo_accounts_use_case() is not None

    def test_login_and_resolution_share_one_user_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both use cases must see the same repository instance, or a seeded
        # account could log in and then fail to resolve.
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        container = Container(settings=Settings())
        assert container.user_repository is container.user_repository

    def test_a_production_container_without_a_secret_does_not_construct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("DATABASE__URL", _DUMMY_DB_URL)
        with pytest.raises(ConfigurationError, match="AUTH__SECRET_KEY"):
            Container(settings=Settings())

    def test_a_production_container_without_a_database_does_not_construct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # In-memory ownership in production would mean authorization decisions
        # served from a store that forgets on restart.
        _isolate_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        with pytest.raises(ConfigurationError, match="DATABASE__URL"):
            Container(settings=Settings())


class TestSeedingGuards:
    async def test_seeding_is_skipped_without_a_demo_password(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # No fallback password exists, so the accounts simply are not created.
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        container = Container(settings=Settings())

        with caplog.at_level(logging.WARNING, logger="app.core.container"):
            assert await container.seed_demo_accounts() is None
        assert "AUTH__DEMO_PASSWORD is not set" in caplog.text

    async def test_seeding_is_skipped_in_production(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", "dev-only-password-1234")
        monkeypatch.setenv("DATABASE__URL", _DUMMY_DB_URL)
        container = Container(settings=Settings())

        with caplog.at_level(logging.WARNING, logger="app.core.container"):
            assert await container.seed_demo_accounts() is None
        assert "production" in caplog.text

    async def test_seeding_is_skipped_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", "dev-only-password-1234")
        monkeypatch.setenv("AUTH__SEED_DEMO_ACCOUNTS", "false")
        container = Container(settings=Settings())
        assert await container.seed_demo_accounts() is None

    async def test_seeding_creates_the_accounts_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", "dev-only-password-1234")
        container = Container(settings=Settings())

        with caplog.at_level(logging.INFO, logger="app.core.container"):
            result = await container.seed_demo_accounts()

        assert result is not None
        assert len(result.created) == 3
        # Emails and counts are logged; the password is not.
        assert "dev-only-password-1234" not in caplog.text

    async def test_seeding_twice_in_one_process_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_env(monkeypatch)
        monkeypatch.setenv("AUTH__SECRET_KEY", "x" * MIN_SECRET_LENGTH)
        monkeypatch.setenv("AUTH__DEMO_PASSWORD", "dev-only-password-1234")
        container = Container(settings=Settings())

        first = await container.seed_demo_accounts()
        second = await container.seed_demo_accounts()
        assert first is not None and second is not None
        assert len(first.created) == 3
        assert second.created == ()
        assert len(second.skipped) == 3


def test_get_container_is_cached() -> None:
    get_container.cache_clear()
    get_settings.cache_clear()
    try:
        assert get_container() is get_container()
    finally:
        get_container.cache_clear()
        get_settings.cache_clear()
