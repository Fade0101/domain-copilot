"""Deployment configuration keeps RRF/BGE confidence semantics explicit and bounded."""

import pytest
from pydantic import ValidationError

from app.core.config import RerankerSettings, RetrievalSettings, Settings


def test_hybrid_defaults_are_rrf_sixty_and_normalized_reranker_threshold() -> None:
    settings = RetrievalSettings()
    assert settings.rrf_k == 60
    assert settings.score_threshold == 0.5
    assert settings.candidate_k == 20 and settings.top_k == 5


@pytest.mark.parametrize(
    "values",
    [
        {"rrf_k": 0},
        {"candidate_k": 101},
        {"top_k": 0},
        {"score_threshold": -0.1},
        {"score_threshold": 1.1},
        {"score_threshold": float("nan")},
        {"max_context_characters": 0},
        {"timeout_seconds": -1},
    ],
)
def test_invalid_hybrid_configuration_is_rejected(values) -> None:
    with pytest.raises(ValidationError):
        RetrievalSettings(**values)


@pytest.mark.parametrize("values", [{"batch_size": 0}, {"max_length": 8193}, {"device": "remote"}])
def test_invalid_local_reranker_configuration_is_rejected(values) -> None:
    with pytest.raises(ValidationError):
        RerankerSettings(**values)


def test_hybrid_overrides_use_the_existing_nested_settings_loader(monkeypatch) -> None:
    monkeypatch.setenv("RETRIEVAL__RRF_K", "70")
    monkeypatch.setenv("RETRIEVAL__SCORE_THRESHOLD", "0.7")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.retrieval.rrf_k == 70 and settings.retrieval.score_threshold == 0.7
