"""Custom sequential clinical workflow orchestrator (Ticket #17).

Coordinates:
Researcher (#14) -> Safety Checker (#15) -> Drafter (#16) -> Approval Gate (#19) -> Finalizer (#18).

Hard Invariants:
1. Safety Checker is mandatory and cannot be skipped.
2. APPROVED is an approval decision outcome (#19), NOT a workflow state.
3. At approval pause, JobPaused is raised to release the execution worker slot.
4. Completed stages are checkpointed and never rerun on resume.
5. Control limits: <= 10 iterations, <= 60s per step, <= 3 retries.
6. RAG fallback is informational only and fails closed for clinical documentation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from app.application.agents.contracts import CaseSummary
from app.application.agents.documentation_drafter import DocumentationDrafterAgent
from app.application.agents.guideline_researcher import GuidelineResearcherAgent
from app.application.agents.safety_checker import SafetyCheckerAgent
from app.application.approvals.contracts import DraftReview
from app.application.approvals.service import ApprovalService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    ToolName,
)
from app.application.clinical_tools.execution import ClinicalToolFactory
from app.application.errors import JobPaused, UnknownPrincipalError
from app.application.ports.jobs import IJobContext
from app.application.ports.llm import ToolCall
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock
from app.application.ports.workflow import IWorkflowRunRepository
from app.application.workflow.codecs import (
    decode_clinical_note_draft,
    decode_research_findings,
    decode_safety_verdict,
    encode_citation,
    encode_clinical_note_draft,
    encode_research_findings,
    encode_safety_verdict,
)
from app.application.workflow.fallback import InformationalRagFallback
from app.domain.approvals.entities import ApprovalStatus
from app.domain.auth.value_objects import UserId
from app.domain.shared.errors import ApprovalRequiredError
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.errors import (
    SafetyCheckRequiredError,
    WorkflowStepTimeoutError,
)
from app.domain.workflow.policy import (
    MAX_STEP_RETRIES,
    MAX_STEP_TIMEOUT_SECONDS,
    MAX_WORKFLOW_ITERATIONS,
    WorkflowPolicy,
)
from app.domain.workflow.state import ClinicalWorkflowState


class ClinicalWorkflowOrchestrator:
    def __init__(
        self,
        *,
        researcher_factory: Callable[[Principal, UUID], GuidelineResearcherAgent],
        safety_checker_factory: Callable[[Principal, UUID], SafetyCheckerAgent],
        drafter_factory: Callable[[Principal, UUID], DocumentationDrafterAgent],
        tool_factory: ClinicalToolFactory,
        approval_service: ApprovalService,
        workflow_repo: IWorkflowRunRepository,
        user_repo: IUserRepository,
        fallback: InformationalRagFallback,
        clock: IClock,
        policy: WorkflowPolicy | None = None,
    ) -> None:
        self._researcher_factory = researcher_factory
        self._safety_checker_factory = safety_checker_factory
        self._drafter_factory = drafter_factory
        self._tool_factory = tool_factory
        self._approvals = approval_service
        self._workflows = workflow_repo
        self._users = user_repo
        self._fallback = fallback
        self._clock = clock
        self._policy = policy or WorkflowPolicy(
            max_iterations=MAX_WORKFLOW_ITERATIONS,
            step_timeout_seconds=MAX_STEP_TIMEOUT_SECONDS,
            max_step_retries=MAX_STEP_RETRIES,
        )

    async def _resolve_principal(self, user_id: UUID) -> Principal:
        user = await self._users.get_by_id(UserId(str(user_id)))
        if user is None:
            raise UnknownPrincipalError(f"User {user_id} not found.")
        return Principal.from_user(user)

    async def run(self, context: IJobContext) -> dict[str, Any]:
        """Execute the sequential clinical workflow inside the checkpointed JobContext."""
        payload = context.payload
        principal = await self._resolve_principal(context.user_id)
        now = self._clock.now()

        # 1. Establish WorkflowRun identity and binding
        raw_workflow_id = payload.get("workflow_id")
        workflow_id = (
            UUID(raw_workflow_id)
            if raw_workflow_id
            else context.correlation_id or uuid4()
        )
        case_summary_text = payload["case_summary"]
        clinical_question_text = payload["clinical_question"]
        patient_context_text = payload.get("patient_context", "")

        workflow = await self._workflows.get_by_id(workflow_id)
        if workflow is None:
            workflow = WorkflowRun(
                id=workflow_id,
                user_id=context.user_id,
                correlation_id=str(context.correlation_id or workflow_id),
                case_summary=case_summary_text,
                state=ClinicalWorkflowState.RESEARCH,
                approval_job_id=context.job_id,
                created_at=now,
                updated_at=now,
            )
            await self._workflows.save(workflow)
        elif workflow.approval_job_id is None:
            workflow = workflow.bind_job(context.job_id, now)
            await self._workflows.save(workflow)

        case = CaseSummary(
            workflow_id=workflow_id,
            clinical_question=clinical_question_text,
            case_summary=case_summary_text,
            patient_context=patient_context_text,
        )

        iteration_count = 0

        def record_iteration() -> None:
            nonlocal iteration_count
            iteration_count += 1
            self._policy.check_iteration(iteration_count)

        # -------------------------------------------------------------------
        # STAGE 1: RESEARCH (#14)
        # -------------------------------------------------------------------
        async def execute_research() -> dict[str, Any]:
            record_iteration()
            researcher = self._researcher_factory(principal, workflow_id)
            retries = 0
            while True:
                start_time = time.monotonic()
                try:
                    async with asyncio.timeout(self._policy.step_timeout_seconds):
                        findings = await researcher.execute(case)
                    elapsed = time.monotonic() - start_time
                    self._policy.check_timeout(elapsed)
                    return {
                        "findings": encode_research_findings(findings),
                        "iterations": iteration_count,
                        "failed": False,
                    }
                except (TimeoutError, WorkflowStepTimeoutError):
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    fallback_result = await self._fallback.retrieve_context(
                        case.clinical_question,
                        principal,
                        failure_reason="Research step timed out.",
                    )
                    return {
                        "findings": None,
                        "iterations": iteration_count,
                        "failed": True,
                        "error": "TIMEOUT",
                        "fallback": {
                            "context": fallback_result.context,
                            "citations": [encode_citation(c) for c in fallback_result.citations],
                            "refusal_reason": fallback_result.refusal_reason,
                        },
                    }
                except Exception as exc:
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    fallback_result = await self._fallback.retrieve_context(
                        case.clinical_question, principal, failure_reason=str(exc)
                    )
                    return {
                        "findings": None,
                        "iterations": iteration_count,
                        "failed": True,
                        "error": str(exc),
                        "fallback": {
                            "context": fallback_result.context,
                            "citations": [
                                encode_citation(c) for c in fallback_result.citations
                            ],
                            "refusal_reason": fallback_result.refusal_reason,
                        },
                    }

        research_step_data = await context.step("research", execute_research)
        iteration_count = max(iteration_count, research_step_data.get("iterations", 0))

        if research_step_data.get("failed"):
            await self._workflows.update_state(
                workflow_id, ClinicalWorkflowState.FAILED, self._clock.now()
            )
            return {
                "workflow_id": str(workflow_id),
                "workflow_state": ClinicalWorkflowState.FAILED.value,
                "refused": True,
                "error": research_step_data.get("error"),
                "fallback": research_step_data.get("fallback"),
            }

        if not research_step_data.get("findings"):
            raise SafetyCheckRequiredError("Research produced no findings.")

        findings = decode_research_findings(research_step_data["findings"])

        if workflow.state == ClinicalWorkflowState.RESEARCH:
            workflow = workflow.transition(ClinicalWorkflowState.SAFETY_CHECK, self._clock.now())
            await self._workflows.update_state(
                workflow_id, ClinicalWorkflowState.SAFETY_CHECK, self._clock.now()
            )

        # -------------------------------------------------------------------
        # STAGE 2: SAFETY CHECK (#15) — MANDATORY GATE
        # -------------------------------------------------------------------
        await context.check_cancelled()

        async def execute_safety() -> dict[str, Any]:
            record_iteration()
            safety_checker = self._safety_checker_factory(principal, workflow_id)
            retries = 0
            while True:
                start_time = time.monotonic()
                try:
                    async with asyncio.timeout(self._policy.step_timeout_seconds):
                        verdict = await safety_checker.execute(findings)
                    elapsed = time.monotonic() - start_time
                    self._policy.check_timeout(elapsed)
                    return {
                        "verdict": encode_safety_verdict(verdict),
                        "iterations": iteration_count,
                    }
                except (TimeoutError, WorkflowStepTimeoutError):
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    raise
                except Exception:
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    raise

        safety_step_data = await context.step("safety_check", execute_safety)
        iteration_count = max(iteration_count, safety_step_data.get("iterations", 0))

        if not safety_step_data.get("verdict"):
            raise SafetyCheckRequiredError("Safety Checker produced no verdict.")

        verdict = decode_safety_verdict(safety_step_data["verdict"])
        if not verdict.can_proceed:
            # Unsafe or flagged -> fail closed immediately
            await self._workflows.update_state(
                workflow_id, ClinicalWorkflowState.FAILED, self._clock.now()
            )
            return {
                "workflow_id": str(workflow_id),
                "workflow_state": ClinicalWorkflowState.FAILED.value,
                "refused": True,
                "violations": [f.reason for f in verdict.flags] or list(verdict.reasons),
                "reasoning": "; ".join(verdict.reasons),
            }

        if workflow.state == ClinicalWorkflowState.SAFETY_CHECK:
            workflow = workflow.transition(ClinicalWorkflowState.DRAFT, self._clock.now())
            await self._workflows.update_state(
                workflow_id, ClinicalWorkflowState.DRAFT, self._clock.now()
            )

        # -------------------------------------------------------------------
        # STAGE 3: DRAFT (#16)
        # -------------------------------------------------------------------
        await context.check_cancelled()

        async def execute_draft() -> dict[str, Any]:
            record_iteration()
            drafter = self._drafter_factory(principal, workflow_id)
            retries = 0
            while True:
                start_time = time.monotonic()
                try:
                    async with asyncio.timeout(self._policy.step_timeout_seconds):
                        draft = await drafter.execute(verdict, case)
                    elapsed = time.monotonic() - start_time
                    self._policy.check_timeout(elapsed)
                    return {
                        "draft": encode_clinical_note_draft(draft),
                        "iterations": iteration_count,
                    }
                except (TimeoutError, WorkflowStepTimeoutError):
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    raise
                except Exception:
                    if self._policy.is_retry_permitted(retries):
                        retries += 1
                        record_iteration()
                        continue
                    raise

        draft_step_data = await context.step("draft", execute_draft)
        iteration_count = max(iteration_count, draft_step_data.get("iterations", 0))

        if not draft_step_data.get("draft"):
            raise SafetyCheckRequiredError("Documentation Drafter produced no draft.")

        draft = decode_clinical_note_draft(draft_step_data["draft"])

        if workflow.state == ClinicalWorkflowState.DRAFT:
            workflow = workflow.transition(
                ClinicalWorkflowState.AWAITING_APPROVAL, self._clock.now()
            )
            await self._workflows.update_state(
                workflow_id, ClinicalWorkflowState.AWAITING_APPROVAL, self._clock.now()
            )

        # -------------------------------------------------------------------
        # STAGE 4: APPROVAL GATE PAUSE (#19)
        # -------------------------------------------------------------------
        await context.check_cancelled()

        async def prepare_review() -> dict[str, Any]:
            snapshot = DraftReview(draft=draft, safety_verdict=verdict)
            await self._approvals.prepare_review(snapshot, context.job_id, principal)
            return {
                "review_prepared": True,
                "draft_id": draft.draft_id,
                "iterations": iteration_count,
            }

        await context.step("approval_prepared", prepare_review)

        # Check if a human decision has already been recorded (#19)
        decision = await self._approvals.get_decision(workflow_id, principal)
        if decision is None:
            # Human has not approved yet -> raise JobPaused to release worker slot
            raise JobPaused()

        if decision.status == ApprovalStatus.REJECTED:
            workflow = workflow.transition(
                ClinicalWorkflowState.REJECTED,
                self._clock.now(),
                approval_decision="REJECTED",
            )
            await self._workflows.update_state(
                workflow_id,
                ClinicalWorkflowState.REJECTED,
                self._clock.now(),
                approval_decision="REJECTED",
            )
            return {
                "workflow_id": str(workflow_id),
                "workflow_state": ClinicalWorkflowState.REJECTED.value,
                "refused": True,
                "approval_id": str(decision.id),
                "rejection_reason": decision.reason,
            }

        if decision.status == ApprovalStatus.APPROVED:
            workflow = workflow.transition(
                ClinicalWorkflowState.FINALIZE,
                self._clock.now(),
                approval_decision="APPROVED",
            )
            await self._workflows.update_state(
                workflow_id,
                ClinicalWorkflowState.FINALIZE,
                self._clock.now(),
                approval_decision="APPROVED",
            )

        # -------------------------------------------------------------------
        # STAGE 5: FINALIZE (#18)
        # -------------------------------------------------------------------
        await context.check_cancelled()

        if decision is None or decision.status != ApprovalStatus.APPROVED:
            raise ApprovalRequiredError(
                "A persisted APPROVED decision is required to finalize."
            )

        async def execute_finalization() -> dict[str, Any]:
            record_iteration()
            executor = self._tool_factory.for_orchestrator(principal, workflow_id)
            final_input = {
                "workflow_id": str(workflow_id),
                "approval_id": str(decision.id),
                "draft_id": str(decision.draft_id),
            }
            call = ToolCall(
                id=f"fin-{uuid4().hex[:8]}",
                name=ToolName.FINALIZE_CLINICAL_NOTE.value,
                arguments=json.dumps(final_input),
            )
            result = await executor.execute(call)
            output = json.loads(result.output)
            if not output.get("ok"):
                raise RuntimeError(f"Finalization failed: {output.get('error')}")
            return {
                "finalized": True,
                "note_id": output["result"]["note_id"],
                "iterations": iteration_count,
            }

        final_step_data = await context.step("finalize", execute_finalization)
        workflow = workflow.transition(ClinicalWorkflowState.COMPLETED, self._clock.now())
        await self._workflows.update_state(
            workflow_id, ClinicalWorkflowState.COMPLETED, self._clock.now()
        )

        return {
            "workflow_id": str(workflow_id),
            "workflow_state": ClinicalWorkflowState.COMPLETED.value,
            "note_id": final_step_data["note_id"],
            "draft_id": decision.draft_id,
            "approval_id": str(decision.id),
        }
