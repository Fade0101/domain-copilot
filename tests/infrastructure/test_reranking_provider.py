"""Cross-encoder contract, local model identity, failure mapping and bounded inference."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pytest

from app.application.errors import ProviderError, ProviderUnavailableError
from app.application.ports.reranking import IReranker
from app.infrastructure.reranking.local_adapter import (
    MODEL_NAME,
    MODEL_REVISION,
    LocalCrossEncoderReranker,
)
from tests.support.knowledge_fakes import hit

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


class PredictModel:
    def __init__(self, values=None) -> None:
        self.values = values if values is not None else [0.8, 0.2]
        self.pairs = None
        self.options: dict[str, Any] = {}

    def predict(self, pairs, **options):
        self.pairs, self.options = pairs, options
        return np.array(self.values)


async def test_adapter_scores_query_chunk_pairs_with_explicit_sigmoid() -> None:
    model = PredictModel()
    adapter = LocalCrossEncoderReranker(model=cast("CrossEncoder", model))
    try:
        result = await adapter.rerank("clinic", [hit(1), hit(2)])
        assert isinstance(adapter, IReranker)
        assert model.pairs == [("clinic", hit(1).snippet), ("clinic", hit(2).snippet)]
        assert type(model.options["activation_fn"]).__name__ == "Sigmoid"
        assert model.options["apply_softmax"] is False
        assert [(item.chunk_id, item.score) for item in result] == [
            (hit(1).chunk_id, 0.8),
            (hit(2).chunk_id, 0.2),
        ]
    finally:
        adapter.close()


async def test_empty_input_does_not_load_model() -> None:
    adapter = LocalCrossEncoderReranker()
    try:
        assert adapter.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert await adapter.rerank("clinic", []) == []
        assert adapter._model is None
    finally:
        adapter.close()


async def test_lazy_loader_pins_bge_revision_and_disables_remote_code(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def build(name, **options):
        captured.update(name=name, **options)
        return PredictModel([0.9])

    module = ModuleType("sentence_transformers")
    setattr(module, "CrossEncoder", build)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    adapter = LocalCrossEncoderReranker()
    try:
        await adapter.rerank("clinic", [hit(1)])
        assert captured["name"] == MODEL_NAME == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert captured["revision"] == MODEL_REVISION
        assert len(MODEL_REVISION) == 40
        assert captured["trust_remote_code"] is False and captured["device"] == "cpu"
    finally:
        adapter.close()


@pytest.mark.parametrize("values", [[], [float("nan")], [float("inf")], [-1], [2]])
async def test_invalid_model_scores_raise_typed_error(values) -> None:
    adapter = LocalCrossEncoderReranker(model=cast("CrossEncoder", PredictModel(values)))
    try:
        with pytest.raises(ProviderError):
            await adapter.rerank("clinic", [hit(1)])
    finally:
        adapter.close()


async def test_model_exception_is_translated_without_exposing_internal_detail() -> None:
    class BrokenModel:
        def predict(self, *args, **kwargs):
            raise RuntimeError("private model cache path")

    adapter = LocalCrossEncoderReranker(model=cast("CrossEncoder", BrokenModel()))
    try:
        with pytest.raises(ProviderUnavailableError, match="^Local cross-encoder is unavailable$"):
            await adapter.rerank("clinic", [hit(1)])
    finally:
        adapter.close()


async def test_cancellation_does_not_allow_unbounded_queued_model_work() -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowModel:
        def predict(self, *args, **kwargs):
            entered.set()
            assert release.wait(10)
            return np.array([0.9])

    adapter = LocalCrossEncoderReranker(model=cast("CrossEncoder", SlowModel()))
    task = asyncio.create_task(adapter.rerank("clinic", [hit(1)]))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProviderUnavailableError, match="busy"):
            await adapter.rerank("clinic", [hit(2)])
    finally:
        release.set()
        adapter.close()
