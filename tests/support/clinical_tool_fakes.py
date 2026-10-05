"""Routing/evidence test helpers. Approval security is tested with real PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    FinalizeClinicalNoteInput,
    FinalizeClinicalNoteOutput,
    ToolName,
    ToolOwner,
    draft_digest,
)
from app.application.clinical_tools.execution import ClinicalToolExecutor, ClinicalToolFactory
from app.application.clinical_tools.read_tools import ReadClinicalTools
from app.application.ports.clinical_tools import IFinalClinicalNoteWriter
from app.application.retrieval.observability import RetrievalObserver
from app.domain.auth.value_objects import ResourceType, Role, UserId
from app.infrastructure.clinical_tools.contracts import ClinicalToolContracts
from app.infrastructure.system.identifiers import UuidGenerator
from tests.support.fakes import FakeOwnershipQuery, FakeUserRepository, FixedClock, build_user
from tests.support.knowledge_fakes import KnowledgeHarness, harness

NOW = datetime(2026, 10, 5, tzinfo=UTC)
ORIGINAL_NOTE = "Synthetic draft for tool boundary tests; no patient information."
REVIEWED_NOTE = "Synthetic clinician-reviewed final text."
DOSE_CLAIM = "Synthex-A dosage for the synthetic adult example is 5 mg once daily."


class RoutingFinalizer(IFinalClinicalNoteWriter):
    """Counts dispatch only; it makes no assertion about the real approval guard."""

    def __init__(self) -> None:
        self.calls: list[FinalizeClinicalNoteInput] = []

    async def finalize(
        self, request: FinalizeClinicalNoteInput, actor_id: UserId
    ) -> FinalizeClinicalNoteOutput:
        self.calls.append(request)
        return FinalizeClinicalNoteOutput(
            uuid4(),
            request.workflow_id,
            request.draft_id,
            request.approval_id,
            REVIEWED_NOTE,
            UUID(actor_id.value),
            NOW,
            True,
        )


def arguments(name: ToolName, workflow_id: UUID, approval_id: UUID) -> dict[str, object]:
    by_tool: dict[ToolName, dict[str, object]] = {
        ToolName.SEARCH_CORPUS: {"query": "synthetic corpus guidance"},
        ToolName.RETRIEVE_DRUG_INFO: {"drug": "Synthex-A", "topic": "overview"},
        ToolName.CHECK_INTERACTIONS: {"drugs": ["Synthex-A", "Synthex-B"]},
        ToolName.VALIDATE_DOSAGE: {"drug": "Synthex-A", "dosage_claim": DOSE_CLAIM},
        ToolName.DRAFT_CLINICAL_NOTE: {
            "clinical_question": "synthetic corpus guidance",
            "case_summary": "Clearly synthetic test context, no patient information.",
        },
        ToolName.FINALIZE_CLINICAL_NOTE: {
            "workflow_id": str(workflow_id),
            "approval_id": str(approval_id),
            "draft_id": draft_digest(ORIGINAL_NOTE),
        },
    }
    return by_tool[name]


def bind(
    factory: ClinicalToolFactory, owner: ToolOwner, principal: Principal, workflow_id: UUID
) -> ClinicalToolExecutor:
    # Test selection only; production wiring uses a literal factory method.
    return {
        ToolOwner.GUIDELINE_RESEARCHER: factory.for_guideline_researcher,
        ToolOwner.SAFETY_CHECKER: factory.for_safety_checker,
        ToolOwner.DOCUMENTATION_DRAFTER: factory.for_documentation_drafter,
        ToolOwner.ORCHESTRATOR: factory.for_orchestrator,
    }[owner](principal, workflow_id)


@dataclass
class ToolHarness:
    factory: ClinicalToolFactory
    principal: Principal
    workflow_id: UUID
    approval_id: UUID
    knowledge: KnowledgeHarness
    finalizer: RoutingFinalizer
    users: FakeUserRepository
    ownership: FakeOwnershipQuery


def tool_harness(
    role: Role = Role.ANALYST, knowledge: KnowledgeHarness | None = None
) -> ToolHarness:
    knowledge = knowledge or harness()
    user = build_user(user_id=str(uuid4()), email="synthetic@example.com", role=role)
    users = FakeUserRepository([user])
    ownership = FakeOwnershipQuery()
    workflow = uuid4()
    ownership.register(ResourceType.RUN, str(workflow), user.id)
    finalizer = RoutingFinalizer()
    factory = ClinicalToolFactory(
        users,
        AuthorizationService(ownership),
        ClinicalToolContracts(),
        ReadClinicalTools(knowledge.retrieval, knowledge.ask),
        finalizer,
        RetrievalObserver(knowledge.audit, FixedClock(NOW)),
        UuidGenerator(),
    )
    return ToolHarness(
        factory,
        Principal.from_user(user),
        workflow,
        uuid4(),
        knowledge,
        finalizer,
        users,
        ownership,
    )
