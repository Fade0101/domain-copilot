"""One real T7 step per case, with immutable PG artifacts and resume pins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import JobCancelled, JobStoreError
from app.application.evaluation.data import (
    EVALUATION_OPERATION,
    EvaluationSetupError,
    canonical_hash,
)
from app.application.evaluation.metrics import assess_case, summarize
from app.application.evaluation.probe import EvaluationProbe
from app.application.ports.evaluation import (
    IEvaluationArtifacts,
    IEvaluationCatalog,
    IEvaluationEvidence,
    IEvaluationVersions,
)
from app.application.ports.jobs import IJobContext, IJobStore
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock
from app.domain.auth.value_objects import Permission, UserId
from app.domain.shared.errors import InvariantViolationError


class EvaluationJobHandler:
    operation_type = EVALUATION_OPERATION

    def __init__(
        self,
        catalog: IEvaluationCatalog,
        evidence: IEvaluationEvidence,
        artifacts: IEvaluationArtifacts,
        versions: IEvaluationVersions,
        users: IUserRepository,
        authorization: AuthorizationService,
        jobs: IJobStore,
        probe: Callable[[], EvaluationProbe],
        clock: IClock,
    ) -> None:
        self._catalog = catalog
        self._evidence = evidence
        self._artifacts = artifacts
        self._versions = versions
        self._users = users
        self._authorization = authorization
        self._jobs = jobs
        self._probe = probe
        self._clock = clock

    def validate(self, payload: dict[str, Any]) -> None:
        pins = self._catalog.load().pins
        if set(payload) - {"pins", "resume_from"} or payload.get("pins") != pins:
            raise InvariantViolationError(
                "Evaluation must use the installed golden set and corpus pins."
            )
        if payload.get("resume_from") is not None:
            try:
                UUID(payload["resume_from"])
            except (ValueError, TypeError, AttributeError):
                raise InvariantViolationError("Invalid evaluation restart reference.") from None

    async def _principal(self, context: IJobContext) -> Principal:
        user = await self._users.get_by_id(UserId(str(context.user_id)))
        if user is None:
            raise EvaluationSetupError("EVALUATION_USER_MISSING")
        principal = Principal.from_user(user)
        self._authorization.require_permission(principal, Permission.MANAGE_ALL_JOBS)
        return principal

    async def run(self, context: IJobContext) -> dict[str, Any]:
        try:
            return await self._run(context)
        except (JobStoreError, JobCancelled):
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, EvaluationSetupError) else type(exc).__name__
            await self._artifacts.write(
                context.job_id, "failure", {"code": code}, self._clock.now()
            )
            raise

    async def _run(self, context: IJobContext) -> dict[str, Any]:
        dataset = self._catalog.load()
        await self._principal(context)
        snapshot = await self._evidence.snapshot(dataset)
        versions = await self._versions.capture()
        # The query embedder must address exactly the vectors #8 wrote. Also
        # record every document's actual chunking settings, not assumed defaults.
        for config in snapshot.ingestion_config.values():
            embedding = versions["embedding"]
            if any(
                config.get("embedding_" + key) != embedding[key]
                for key in ("model", "version", "dim")
            ):
                raise EvaluationSetupError("EMBEDDING_PROVENANCE_MISMATCH")
        for case in dataset.cases:
            snapshot.expected_chunks(case)
        run_pins = {
            **dataset.pins,
            "index_sha256": snapshot.fingerprint,
            "runtime_sha256": canonical_hash(versions),
        }
        parent_id = (
            UUID(context.payload["resume_from"]) if context.payload.get("resume_from") else None
        )
        if parent_id is not None:
            parent = await self._jobs.get(parent_id)
            if (
                parent is None
                or not parent.terminal
                or parent.operation_type != EVALUATION_OPERATION
            ):
                raise EvaluationSetupError("INVALID_RESTART_PARENT")
            previous = await self._artifacts.read(parent_id, "started")
            if previous is None or previous["pins"] != run_pins:
                raise EvaluationSetupError("RESTART_PINS_CHANGED")

        async def start() -> dict[str, Any]:
            saved = await self._artifacts.read(context.job_id, "started")
            if saved is None:
                saved = {
                    "job_id": str(context.job_id),
                    "timestamp": self._clock.now().isoformat(),
                    "pins": run_pins,
                    "versions": versions,
                    "document_ids": snapshot.documents,
                    "chunk_count": len(snapshot.chunks),
                    "ingestion_config": snapshot.ingestion_config,
                    "case_ids": [case.id for case in dataset.cases],
                    "dataset_composition": {
                        "qa": sum(not case.adversarial for case in dataset.cases),
                        "adversarial": sum(case.adversarial for case in dataset.cases),
                        "injection": sum("injection" in case.safety_tags for case in dataset.cases),
                    },
                    "resume_from": str(parent_id) if parent_id else None,
                }
                await self._artifacts.write(context.job_id, "started", saved, self._clock.now())
            if saved["pins"] != run_pins:
                raise EvaluationSetupError("RESUME_PINS_CHANGED")
            return {"pins": run_pins, "total_cases": len(dataset.cases)}

        initial = await context.step("evaluation/started", start)
        if initial["pins"] != run_pins:
            raise EvaluationSetupError("RESUME_PINS_CHANGED")
        probe = self._probe()
        try:
            results: list[dict[str, Any]] = []
            for case in dataset.cases:
                key = "case/" + case.id

                async def evaluate() -> dict[str, Any]:
                    saved = await self._artifacts.read(context.job_id, key)
                    if saved is None and parent_id is not None:
                        saved = await self._artifacts.read(parent_id, key)
                        if saved is not None:
                            saved = {**saved, "reused_from_job_id": str(parent_id)}
                    if saved is None:
                        principal = await self._principal(context)
                        # Detect corpus drift instead of mixing two indexes in one report.
                        current = await self._evidence.snapshot(dataset)
                        if current.fingerprint != snapshot.fingerprint:
                            raise EvaluationSetupError("CORPUS_CHANGED_DURING_EVALUATION")
                        observed = await probe.execute(case.query, principal)
                        saved = assess_case(case, observed, snapshot)
                    await self._artifacts.write(context.job_id, key, saved, self._clock.now())
                    return {"case_id": case.id, "passed": saved["passed"]}

                await context.step("evaluation/" + key, evaluate)
                saved = await self._artifacts.read(context.job_id, key)
                if saved is None:
                    raise EvaluationSetupError("CHECKPOINT_ARTIFACT_MISSING")
                results.append(saved)

            async def finish() -> dict[str, Any]:
                current = await self._evidence.snapshot(dataset)
                if current.fingerprint != snapshot.fingerprint:
                    raise EvaluationSetupError("CORPUS_CHANGED_DURING_EVALUATION")
                if canonical_hash(await self._versions.capture()) != run_pins["runtime_sha256"]:
                    raise EvaluationSetupError("RUNTIME_CHANGED_DURING_EVALUATION")
                return {
                    **summarize(results, len(dataset.cases)),
                    "pins": run_pins,
                    "report_available": True,
                }

            # A cancellation arriving during the last case is seen before completion.
            return await context.step("evaluation/report", finish)
        finally:
            await probe.aclose()
