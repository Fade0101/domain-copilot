"""Unit tests for the configuration surface (BRD AR-4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings, get_settings


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make ``Settings()`` hermetic: no developer ``.env``, no inherited nested vars.

    ``chdir`` to an empty directory so the relative ``env_file=".env"`` resolves to
    nothing, and drop any nested vars the developer's shell might export so the test
    observes declared defaults rather than the local environment.
    """
    monkeypatch.chdir(tmp_path)
    for prefix in ("LLM__", "EMBEDDING__", "QUEUE__", "RETRIEVAL__", "LIMITS__", "RETRY__"):
        for suffix in (
            "PROVIDER",
            "MODEL",
            "API_KEY",
            "MAX_RETRIES",
            "MAX_ITERATIONS",
            "TOP_K",
        ):
            monkeypatch.delenv(prefix + suffix, raising=False)


def test_defaults_are_populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    settings = Settings()

    assert settings.app_name == "Domain Copilot"
    assert settings.llm.temperature == 0.0
    assert settings.limits.max_iterations == 10
    assert settings.limits.per_step_timeout_seconds == 60
    assert settings.retry.max_retries == 3
    assert settings.retrieval.top_k == 5
    assert settings.prompts.directory == "prompts"
    assert settings.prompts.strict is True


def test_nested_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM__MODEL", "custom-model")
    monkeypatch.setenv("RETRY__MAX_RETRIES", "7")

    settings = Settings()

    assert settings.llm.model == "custom-model"
    assert settings.retry.max_retries == 7


def test_api_key_is_secret_and_defaults_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    assert Settings().llm.api_key is None

    monkeypatch.setenv("LLM__API_KEY", "super-secret-value")
    settings = Settings()

    assert isinstance(settings.llm.api_key, SecretStr)
    assert settings.llm.api_key.get_secret_value() == "super-secret-value"
    # The secret must never appear in repr/str output (constraint C6).
    assert "super-secret-value" not in repr(settings.llm)
    assert "super-secret-value" not in str(settings.llm.api_key)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
