"""Authorized evaluation submission, progress, reports and cooperative controls."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import JobNotFoundError
from app.application.evaluation.data import EVALUATION_OPERATION
from app.application.evaluation.metrics import summarize
from app.application.jobs.service import JobService
from app.application.ports.evaluation import IEvaluationArtifacts, IEvaluationCatalog
from app.application.ports.system import IClock
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.jobs.entities import Job, JobState


class EvaluationService:
    def __init__(
        self,
        jobs: JobService,
        catalog: IEvaluationCatalog,
        artifacts: IEvaluationArtifacts,
        authorization: AuthorizationService,
        clock: IClock,
    ) -> None:
        self._jobs = jobs
        self._catalog = catalog
        self._artifacts = artifacts
        self._authorization = authorization
        self._clock = clock

    async def submit(self, principal: Principal) -> Job:
        self._authorization.require_permission(principal, Permission.MANAGE_ALL_JOBS)
        return await self._jobs.submit(
            EVALUATION_OPERATION,
            {"pins": self._catalog.load().pins},
            user_id=UUID(principal.user_id.value),
        )

    async def _job(self, job_id: UUID, principal: Principal) -> Job:
        await self._authorization.require_resource_access(principal, ResourceType.JOB, str(job_id))
        job = await self._jobs.get(job_id)
        if job.operation_type != EVALUATION_OPERATION:
            raise JobNotFoundError("Evaluation job not found.")
        return job

    async def cancel(self, job_id: UUID, principal: Principal) -> Job:
        self._authorization.require_permission(principal, Permission.MANAGE_ALL_JOBS)
        await self._job(job_id, principal)
        await self._artifacts.request_cancel(job_id, self._clock.now())
        return await self._jobs.get(job_id)

    async def restart(self, job_id: UUID, principal: Principal) -> Job:
        self._authorization.require_permission(principal, Permission.MANAGE_ALL_JOBS)
        job = await self._job(job_id, principal)
        if job.state == JobState.STARTED:
            await self._jobs.resume(job_id)
            return await self._jobs.get(job_id)
        if job.state in {JobState.PENDING, JobState.QUEUED}:
            return await self._jobs.dispatch(job_id)
        payload: dict[str, Any] = {"pins": self._catalog.load().pins}
        if job.state in {JobState.CANCELLED, JobState.FAILED} and await self._artifacts.read(
            job_id, "started"
        ):
            payload["resume_from"] = str(job_id)
        # Terminal records never transition backwards. A completed run starts a
        # fresh measurement; an interrupted terminal run can reuse pinned results.
        return await self._jobs.submit(
            EVALUATION_OPERATION, payload, user_id=UUID(principal.user_id.value)
        )

    async def report(self, job_id: UUID, principal: Principal) -> dict[str, Any]:
        job = await self._job(job_id, principal)
        run = await self._artifacts.read(job_id, "started")
        failure = await self._artifacts.read(job_id, "failure")
        case_ids = run["case_ids"] if run else [case.id for case in self._catalog.load().cases]
        results = []
        for case_id in case_ids:
            result = await self._artifacts.read(job_id, "case/" + case_id)
            if result is not None:
                results.append(result)
        summary = summarize(results, len(case_ids))
        if not job.terminal or job.state != JobState.COMPLETED or failure:
            summary["targets_met"] = False
        return {
            "schema_version": 1,
            "job_id": str(job.id),
            "state": job.state.value,
            "cancellation_requested": job.cancellation_requested,
            "setup_error": failure["code"] if failure else None,
            "run": run,
            "summary": summary,
            "cases": results,
        }

    async def status(self, job_id: UUID, principal: Principal) -> dict[str, Any]:
        job = await self._job(job_id, principal)
        run = await self._artifacts.read(job_id, "started")
        failure = await self._artifacts.read(job_id, "failure")
        completed = sum(key.startswith("evaluation/case/") for key in job.checkpoint_data)
        return {
            "job_id": str(job.id),
            "state": job.state.value,
            "cancellation_requested": job.cancellation_requested,
            "completed_cases": completed,
            "total_cases": len(run["case_ids"]) if run else len(self._catalog.load().cases),
            "error": failure["code"] if failure else job.last_error,
            "result": job.result_payload,
        }
