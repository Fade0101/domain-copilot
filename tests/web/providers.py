"""Deterministic agent/provider I/O around the real workflow and grounded ask."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.application.agents.contracts import CaseSummary, ResearchFindings, TerminationReason
from app.application.auth.context import Principal
from app.application.ports.jobs import IJobContext
from app.application.qa.use_cases import AskResult, AskUseCase
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.application.workflow.fallback import InformationalRagFallback
from app.application.workflow.handler import ClinicalWorkflowJobHandler
from app.application.workflow.orchestrator import ClinicalWorkflowOrchestrator
from app.core.config import Settings
from app.core.container import Container
from app.infrastructure.llm.observed import ObservedLLMProvider
from app.infrastructure.system.identifiers import UuidGenerator
from tests.support.approval_fixtures import review_snapshot
from tests.support.knowledge_fakes import (
    StaticRetrievalStore,
    StubEmbeddings,
    StubLLM,
    StubPrompts,
    StubReranker,
    hit,
)


class BrowserAsk:
    def __init__(self, container: Container) -> None:
        self.container = container

    async def execute(self, question: str, principal: Principal) -> AskResult:
        evidence = (
            []
            if "unknown" in question.casefold()
            else [hit(1, "The synthetic clinic opens on Monday. This example is fictional.")]
        )
        observer = self.container._retrieval_observer
        ids = UuidGenerator()
        retrieval = HybridRetrievalUseCase(
            StaticRetrievalStore(evidence, evidence),
            StubEmbeddings(),
            StubReranker(),
            self.container.authorization_service,
            observer,
            ids,
        )
        provider = ObservedLLMProvider(
            StubLLM(), observer, provider="synthetic", model="browser-test"
        )
        return await AskUseCase(retrieval, provider, StubPrompts(), observer, ids).execute(
            question, principal
        )


class BrowserClinicalHandler(ClinicalWorkflowJobHandler):
    """Use the real #17 orchestrator, #19 store and #18 finalizer in Celery."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, context: IJobContext) -> dict[str, Any]:
        self.validate(context.payload)
        container = Container(self.settings)
        workflow_id = UUID(context.payload["workflow_id"])
        snapshot = review_snapshot(workflow_id)

        class Researcher:
            async def execute(self, case: CaseSummary) -> ResearchFindings:
                return ResearchFindings(
                    case.workflow_id,
                    "Synthetic evidence for the browser journey.",
                    snapshot.draft.citations,
                    False,
                    TerminationReason.SUFFICIENT_EVIDENCE,
                    1,
                    1,
                    (),
                )

        class Safety:
            async def execute(self, *_: Any) -> Any:
                return snapshot.safety_verdict

        class Drafter:
            async def execute(self, *_: Any) -> Any:
                return snapshot.draft

        def research_factory(*_: Any) -> Any:
            return Researcher()

        def safety_factory(*_: Any) -> Any:
            return Safety()

        def drafter_factory(*_: Any) -> Any:
            return Drafter()

        orchestrator = ClinicalWorkflowOrchestrator(
            researcher_factory=research_factory,
            safety_checker_factory=safety_factory,
            drafter_factory=drafter_factory,
            tool_factory=container.clinical_tool_factory(),
            approval_service=container.approval_service(),
            workflow_repo=container.workflow_repository,
            user_repo=container.user_repository,
            fallback=InformationalRagFallback(container.ask_use_case()),
            clock=container._clock,
        )
        try:
            # A synthetic slow request gives browser tests a deterministic window
            # to disconnect/reconnect/cancel a real worker operation.
            if context.payload["case_summary"].casefold().startswith("wait"):
                for _ in range(100):
                    await context.check_cancelled()
                    await asyncio.sleep(0.2)
            return await orchestrator.run(context)
        finally:
            await container.dispose()
