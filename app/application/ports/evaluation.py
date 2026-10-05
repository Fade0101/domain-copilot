"""Evaluation reads existing evidence and persists artifacts beside T7 jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.evaluation.data import EvidenceSnapshot, GoldenSet


class IEvaluationCatalog(Protocol):
    def load(self) -> GoldenSet: ...


class IEvaluationEvidence(Protocol):
    async def snapshot(self, dataset: GoldenSet) -> EvidenceSnapshot: ...


class IEvaluationArtifacts(Protocol):
    async def read(self, job_id: UUID, key: str) -> dict[str, Any] | None: ...

    async def write(
        self, job_id: UUID, key: str, payload: dict[str, Any], now: datetime
    ) -> None: ...

    async def request_cancel(self, job_id: UUID, now: datetime) -> None: ...


class IEvaluationVersions(Protocol):
    async def capture(self) -> dict[str, Any]: ...
