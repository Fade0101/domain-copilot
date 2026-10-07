"""Evaluation reads existing evidence and persists artifacts beside T7 jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.auth.context import Principal
from app.application.evaluation.data import ContainmentFixture, EvidenceSnapshot, GoldenSet


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


class IContainmentState(Protocol):
    """Internal evaluation fixtures/observations, not an agent-callable tool."""

    async def prepare(
        self, job_id: UUID, case_id: str, principal: Principal
    ) -> ContainmentFixture: ...

    async def snapshot(
        self, fixture: ContainmentFixture, principal: Principal
    ) -> dict[str, Any]: ...
