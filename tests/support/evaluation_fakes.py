"""Small data/job fixtures for evaluation semantics, not baseline measurements."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.evaluation.data import (
    EvidenceReference,
    EvidenceSnapshot,
    GoldenCase,
    GoldenSet,
    IndexedEvidence,
)
from app.application.evaluation.handler import EvaluationJobHandler
from app.application.evaluation.probe import EvaluationProbe
from app.application.evaluation.service import EvaluationService
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.application.qa.grounding import REFUSAL
from app.domain.auth.entities import User
from app.domain.auth.value_objects import EmailAddress, ResourceType, Role, UserId
from app.domain.jobs.entities import Job
from tests.support.fakes import (
    FakeOwnershipQuery,
    FakeUserRepository,
    FixedClock,
    SequentialIdGenerator,
)
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

NOW = datetime(2026, 10, 4, tzinfo=UTC)
USER = User(
    UserId(str(UUID(int=500))), EmailAddress("evaluation@example.com"), Role.ADMIN, "test-hash", NOW
)
TEXT = "The fictional clinic records source versions."
CHUNK = IndexedEvidence(
    str(UUID(int=51)), str(UUID(int=52)), "clean", "synthetic.md", "Records", None, TEXT, True
)
SNAPSHOT = EvidenceSnapshot(
    "snapshot-v1",
    {CHUNK.chunk_id: CHUNK},
    {"clean": CHUNK.document_id},
    {"clean": {"embedding_model": "test", "embedding_dim": 384, "embedding_version": "1"}},
)


def case(identifier: str = "qa-one", *, refuse: bool = False) -> GoldenCase:
    return GoldenCase(
        identifier,
        "out_of_corpus" if refuse else "documentation",
        identifier,
        () if refuse else ("clean",),
        (),
        () if refuse else (EvidenceReference("clean", TEXT),),
        () if refuse else (TEXT,),
        "refuse" if refuse else "answer",
        0 if refuse else 1,
        refuse,
        (),
        refuse,
        "Unit fixture",
    )


def observation(*, refused: bool = False, chunk: IndexedEvidence = CHUNK) -> dict[str, Any]:
    citation = {
        k: v
        for k, v in asdict(chunk).items()
        if k in {"chunk_id", "document_id", "document_name", "section", "page"}
    }
    citation.update(text_snippet=chunk.text, relevance_score=0.9)
    return {
        "answer": REFUSAL if refused else "[1] " + chunk.text,
        "citations": [] if refused else [citation],
        "refused": refused,
        "retrieval": {
            "selected_chunk_ids": [] if refused else [chunk.chunk_id],
            "reranked": [] if refused else [{"chunk_id": chunk.chunk_id, "relevance_score": 0.9}],
            "outcome": "completed",
        },
        "generation": {"outcome": "refused" if refused else "completed"},
        "latency_ms": 10.0,
    }


class FakeCatalog:
    def __init__(self) -> None:
        self.dataset = GoldenSet(
            "golden-test",
            "hash",
            "corpus-test",
            "corpus-hash",
            "fixture-hash",
            (),
            (case(), case("qa-two"), case("adv-one", refuse=True)),
        )

    def load(self) -> GoldenSet:
        return self.dataset


class FakeEvidence:
    def __init__(self) -> None:
        self.value = SNAPSHOT

    async def snapshot(self, dataset: GoldenSet) -> EvidenceSnapshot:
        return self.value


class FakeArtifacts:
    def __init__(self, jobs: FakeJobStore) -> None:
        self.values: dict[tuple[UUID, str], dict[str, Any]] = {}
        self.jobs = jobs
        self.crash_after_write: str | None = None
        self.cancel_after_write: str | None = None

    async def read(self, job_id: UUID, key: str) -> dict[str, Any] | None:
        return deepcopy(self.values.get((job_id, key)))

    async def write(self, job_id: UUID, key: str, payload: dict[str, Any], now: datetime) -> None:
        self.values.setdefault((job_id, key), deepcopy(payload))
        if key == self.crash_after_write:
            self.crash_after_write = None
            raise WorkerCrash()
        if key == self.cancel_after_write:
            self.cancel_after_write = None
            await self.request_cancel(job_id, now)

    async def request_cancel(self, job_id: UUID, now: datetime) -> None:
        await self.jobs.request_cancel(job_id, now)


class WorkerCrash(BaseException):
    pass


class FakeVersions:
    def __init__(self) -> None:
        self.value = {"embedding": {"model": "test", "dim": 384, "version": "1"}}

    async def capture(self) -> dict[str, Any]:
        return deepcopy(self.value)


class ControlledProbe(EvaluationProbe):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.overrides: dict[str, dict[str, Any]] = {}
        self.closed = 0

    async def execute(self, query: str, principal: Principal) -> dict[str, Any]:
        self.calls.append(query)
        return deepcopy(self.overrides.get(query, observation(refused=query.startswith("adv"))))

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class EvaluationHarness:
    jobs: JobService
    runner: JobRunner
    store: FakeJobStore
    queue: FakeJobQueue
    artifacts: FakeArtifacts
    catalog: FakeCatalog
    evidence: FakeEvidence
    versions: FakeVersions
    probe: ControlledProbe
    users: FakeUserRepository
    owners: FakeOwnershipQuery
    service: EvaluationService

    async def submit(self) -> Job:
        job = await self.service.submit(Principal.from_user(USER))
        self.owners.register(ResourceType.JOB, str(job.id), USER.id)
        return job


def evaluation_harness() -> EvaluationHarness:
    store = FakeJobStore()
    queue = FakeJobQueue(store)
    catalog, evidence, versions, probe = (
        FakeCatalog(),
        FakeEvidence(),
        FakeVersions(),
        ControlledProbe(),
    )
    artifacts = FakeArtifacts(store)
    users, owners = FakeUserRepository([USER]), FakeOwnershipQuery()
    authorization, clock = AuthorizationService(owners), FixedClock(NOW)
    handler = EvaluationJobHandler(
        catalog, evidence, artifacts, versions, users, authorization, store, lambda: probe, clock
    )
    registry = JobHandlerRegistry([handler])
    jobs = JobService(store, queue, registry, clock, SequentialIdGenerator())
    runner = JobRunner(store, registry, clock)
    service = EvaluationService(jobs, catalog, artifacts, authorization, clock)
    return EvaluationHarness(
        jobs,
        runner,
        store,
        queue,
        artifacts,
        catalog,
        evidence,
        versions,
        probe,
        users,
        owners,
        service,
    )
