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
from app.application.ports.retrieval import IRetrievalStore
from app.core.config import DatabaseSettings, LLMSettings, PromptSettings, Settings
from app.core.container import Container, build_llm_provider
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.ollama_adapter import OllamaAdapter
from app.infrastructure.persistence.sql.retrieval_store import PostgresRetrievalStore

_REPO_ROOT = Path(__file__).resolve().parents[3]


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


def _container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, url: str | None) -> Container:
    """Build a container with an explicit database URL and real prompts.

    ``_settings`` chdir's to an empty tmp_path for hermeticity, which would hide
    the prompts/ directory that the container loads eagerly -- so point at the
    real one.
    """
    settings = _settings(monkeypatch, tmp_path, "groq", None).model_copy(
        update={
            "database": DatabaseSettings(url=url),
            "prompts": PromptSettings(directory=str(_REPO_ROOT / "prompts")),
        }
    )
    return Container(settings=settings)


def test_retrieval_store_is_wired_without_a_running_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adapters are built eagerly, so the store must not connect at build time.

    The URL below points at a port with nothing behind it: construction must
    still succeed, because SQLAlchemy opens no connection until a statement
    runs. If this regresses, app startup and the whole unit suite would silently
    begin to require PostgreSQL.
    """
    container = _container(
        monkeypatch, tmp_path, url="postgresql://nobody:nobody@127.0.0.1:1/nonexistent"
    )

    assert isinstance(container.retrieval_store, PostgresRetrievalStore)
    # Exposed through the port, which is what application code depends on.
    assert isinstance(container.retrieval_store, IRetrievalStore)


def test_retrieval_store_is_absent_without_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlike the auth ports there is no in-memory retrieval fallback: dense
    search needs pgvector and keyword search needs PostgreSQL FTS, so a stand-in
    would answer queries it cannot serve."""
    container = _container(monkeypatch, tmp_path, url=None)

    assert container.database is None
    assert container.retrieval_store is None
