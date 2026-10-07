"""Pinned BAAI/bge-reranker-v2-m3 cross-encoder, loaded lazily on a worker thread."""

from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import TYPE_CHECKING

from app.application.errors import ProviderError, ProviderUnavailableError
from app.application.ports.reranking import IReranker, RerankScore
from app.application.ports.retrieval import SearchHit

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"


class LocalCrossEncoderReranker(IReranker):
    def __init__(
        self,
        *,
        model_name: str | None = None,
        revision: str | None = None,
        device: str = "cpu",
        batch_size: int = 4,
        max_length: int = 1024,
        cache_directory: str | None = None,
        model: CrossEncoder | None = None,
    ) -> None:
        self._model_name = model_name or MODEL_NAME
        self._revision = revision or MODEL_REVISION
        self._device = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._cache_directory = cache_directory
        self._model = model
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reranker")
        # Fail fast under saturation. Cancellation must not let another request
        # queue unlimited inference behind an operation still running in a thread.
        self._available = BoundedSemaphore(1)

    @property
    def model_name(self) -> str:
        return self._model_name

    def _get_model(self) -> CrossEncoder:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(
                self._model_name,
                revision=self._revision,
                device=self._device,
                max_length=self._max_length,
                cache_folder=self._cache_directory,
                trust_remote_code=False,
            )
        return self._model

    def _predict(self, query: str, candidates: list[SearchHit]) -> list[RerankScore]:
        try:
            from torch.nn import Sigmoid

            values = self._get_model().predict(
                [(query, candidate.snippet) for candidate in candidates],
                batch_size=self._batch_size,
                activation_fn=Sigmoid(),
                apply_softmax=False,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            scores = [float(value) for value in values]
            if len(scores) != len(candidates) or any(
                not math.isfinite(value) or not 0 <= value <= 1 for value in scores
            ):
                raise ProviderError("Cross-encoder returned invalid scores")
            return [
                RerankScore(candidate.chunk_id, score)
                for candidate, score in zip(candidates, scores, strict=True)
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError("Local cross-encoder is unavailable") from exc
        finally:
            self._available.release()

    async def rerank(self, query: str, candidates: list[SearchHit]) -> list[RerankScore]:
        if not candidates:
            return []
        if not self._available.acquire(blocking=False):
            raise ProviderUnavailableError("Local cross-encoder is busy")
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, self._predict, query, candidates
            )
        except Exception as exc:
            self._available.release()
            raise ProviderUnavailableError("Local cross-encoder is unavailable") from exc
        # Shield the thread future, not the request. A timed-out request stops
        # waiting, while the semaphore stays held until inference actually ends.
        return await asyncio.shield(future)

    def close(self) -> None:
        self._executor.shutdown(wait=False)
