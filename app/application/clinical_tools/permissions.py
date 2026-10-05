"""Closed, immutable allow-list: exactly six named tools and four owners."""

from dataclasses import dataclass
from types import MappingProxyType

from app.application.clinical_tools.contracts import ToolName, ToolOwner
from app.domain.auth.value_objects import Permission


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    owner: ToolOwner
    permissions: tuple[Permission, ...]
    writes_final_note: bool = False


_READ = (Permission.RUN_WORKFLOW, Permission.ASK_QUESTION)
TOOL_POLICIES = MappingProxyType(
    {
        ToolName.SEARCH_CORPUS: ToolPolicy(ToolOwner.GUIDELINE_RESEARCHER, _READ),
        ToolName.RETRIEVE_DRUG_INFO: ToolPolicy(ToolOwner.GUIDELINE_RESEARCHER, _READ),
        ToolName.CHECK_INTERACTIONS: ToolPolicy(ToolOwner.SAFETY_CHECKER, _READ),
        ToolName.VALIDATE_DOSAGE: ToolPolicy(ToolOwner.SAFETY_CHECKER, _READ),
        ToolName.DRAFT_CLINICAL_NOTE: ToolPolicy(
            ToolOwner.DOCUMENTATION_DRAFTER, (Permission.RUN_WORKFLOW,)
        ),
        # Execution on behalf of an analyst is legitimate AFTER a reviewer's
        # persisted approval. The writer separately checks the persisted reviewer
        # has APPROVE_CLINICAL_NOTE; execution itself is not an approval action.
        ToolName.FINALIZE_CLINICAL_NOTE: ToolPolicy(
            ToolOwner.ORCHESTRATOR, (Permission.RUN_WORKFLOW,), writes_final_note=True
        ),
    }
)
