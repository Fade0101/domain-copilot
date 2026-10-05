"""Ports for tool serialization and the single controlled final-note write."""

from __future__ import annotations

from typing import Any, Protocol

from app.application.clinical_tools.contracts import (
    FinalizeClinicalNoteInput,
    FinalizeClinicalNoteOutput,
    ToolInput,
    ToolName,
    ToolOutput,
)
from app.application.ports.llm import ToolDefinition
from app.domain.auth.value_objects import UserId


class IClinicalToolContracts(Protocol):
    def definition(self, name: ToolName) -> ToolDefinition: ...

    def output_schema(self, name: ToolName) -> dict[str, Any]: ...

    def decode(self, name: ToolName, arguments: str) -> ToolInput: ...

    def encode(self, name: ToolName, result: ToolOutput) -> dict[str, Any]: ...


class IFinalClinicalNoteWriter(Protocol):
    async def finalize(
        self, request: FinalizeClinicalNoteInput, actor_id: UserId
    ) -> FinalizeClinicalNoteOutput:
        """Lock and verify the persisted approval, then write, in ONE transaction.

        Recheck current actor/reviewer RBAC and workflow ownership. Require an
        exact APPROVED decision, matching workflow and original-draft digest,
        and nonempty reviewed text. Copy only that persisted reviewed text. No
        caller-provided note or approval flag is accepted. Replays may return
        the same final note but must still recheck approval; never overwrite it.
        """
        ...
