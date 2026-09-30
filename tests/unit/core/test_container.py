"""Config-driven LLM provider selection in the composition root (BRD AR-2, ADR-007).

These tests prove that configuration alone -- not code at the wiring site -- selects
the primary chat provider and the fallback ordering, and that an unknown provider
name fails safely with the typed ``ProviderConfigurationError``. Adapter internals
(``_primary`` / ``_secondary``) are white-boxed deliberately: verifying *which*
adapter landed in each slot is the whole point of a selection test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.application.errors import ProviderConfigurationError
from app.core.config import LLMSettings, Settings
from app.core.container import build_llm_provider
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.ollama_adapter import OllamaAdapter


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    fallback: str | None,
) -> Settings:
    """Build hermetic ``Settings`` with an explicit ``llm`` group.

    Mirrors ``test_settings._isolate``: ``chdir`` away from any developer ``.env``
    and drop inherited ``LLM__*`` vars so the explicit group is authoritative and
    the selection under test is not perturbed by the local environment.
    """
    monkeypatch.chdir(tmp_path)
    for suffix in ("PROVIDER", "FALLBACK", "MODEL", "API_KEY"):
        monkeypatch.delenv("LLM__" + suffix, raising=False)
    return Settings(llm=LLMSettings(provider=provider, fallback=fallback))


def test_groq_primary_ollama_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = build_llm_provider(_settings(monkeypatch, tmp_path, "groq", "ollama"))

    assert isinstance(provider, FallbackLLMProvider)
    assert isinstance(provider._primary, GroqAdapter)
    assert isinstance(provider._secondary, OllamaAdapter)


def test_config_swaps_primary_and_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Swapping provider/fallback in config swaps primary and secondary adapters."""
    provider = build_llm_provider(_settings(monkeypatch, tmp_path, "ollama", "groq"))

    assert isinstance(provider, FallbackLLMProvider)
    assert isinstance(provider._primary, OllamaAdapter)
    assert isinstance(provider._secondary, GroqAdapter)


def test_blank_fallback_returns_bare_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """None or empty fallback disables the composite -- the primary is used directly."""
    provider_none = build_llm_provider(_settings(monkeypatch, tmp_path, "groq", None))
    assert isinstance(provider_none, GroqAdapter)

    provider_empty = build_llm_provider(_settings(monkeypatch, tmp_path, "groq", ""))
    assert isinstance(provider_empty, GroqAdapter)


def test_fallback_equal_to_primary_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = build_llm_provider(_settings(monkeypatch, tmp_path, "groq", "groq"))
    assert isinstance(provider, GroqAdapter)


def test_unknown_primary_provider_fails_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ProviderConfigurationError):
        build_llm_provider(_settings(monkeypatch, tmp_path, "does-not-exist", None))


def test_unknown_fallback_provider_fails_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ProviderConfigurationError):
        build_llm_provider(_settings(monkeypatch, tmp_path, "groq", "does-not-exist"))
