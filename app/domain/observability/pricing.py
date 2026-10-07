"""Explicitly configured, versioned rates; no invented prices for unknown models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from app.domain.shared.errors import InvariantViolationError


@dataclass(frozen=True, slots=True)
class ModelRate:
    prompt_per_million: float
    completion_per_million: float
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip() or any(
            not isfinite(value) or value < 0
            for value in (self.prompt_per_million, self.completion_per_million)
        ):
            raise InvariantViolationError("Rates require a source/version and finite amounts.")


class CostEstimator:
    def __init__(self, rates: Mapping[tuple[str, str], ModelRate] | None = None) -> None:
        self._rates = dict(rates or {})

    def rate(self, provider: str, model: str) -> ModelRate | None:
        return self._rates.get((provider, model))

    def estimate(
        self, provider: str, model: str, prompt: int | None, completion: int | None
    ) -> float | None:
        rate = self.rate(provider, model)
        if rate is None or prompt is None or completion is None:
            return None
        if prompt < 0 or completion < 0:
            raise InvariantViolationError("Token counts cannot be negative.")
        cost = (
            prompt * rate.prompt_per_million + completion * rate.completion_per_million
        ) / 1_000_000
        return cost if isfinite(cost) else None
