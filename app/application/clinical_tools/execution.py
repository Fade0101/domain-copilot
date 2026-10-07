"""Server-bound tool scopes, fresh RBAC, strict dispatch, and existing audit events.

Only trusted composition code holds ClinicalToolFactory. Agents receive one
bound ClinicalToolExecutor, whose execute() accepts the existing #7 ToolCall;
it never accepts a caller-claimed agent, role, user, or permission list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    CheckInteractionsInput,
    DraftClinicalNoteInput,
    FinalizeClinicalNoteInput,
    FinalizeClinicalNoteOutput,
    RetrieveDrugInfoInput,
    SearchCorpusInput,
    ToolInput,
    ToolName,
    ToolOutput,
    ToolOwner,
    ValidateDosageInput,
    require_text,
    require_uuid,
)
from app.application.clinical_tools.errors import (
    ERROR_MESSAGES,
    ApprovalMismatchError,
    ClinicalToolStoreError,
    FinalNoteConflictError,
    ToolErrorCode,
    ToolInputError,
    ToolOutputError,
    ToolPermissionError,
    UnknownToolError,
)
from app.application.clinical_tools.permissions import TOOL_POLICIES
from app.application.clinical_tools.read_tools import ReadClinicalTools
from app.application.errors import (
    AuthenticationError,
    AuthorizationError,
    KnowledgeUnavailableError,
    ResourceNotFoundError,
    UnknownPrincipalError,
)
from app.application.observability.context import get_current_correlation_id, trace_scope
from app.application.ports.audit import AuditEntry
from app.application.ports.clinical_tools import IClinicalToolContracts, IFinalClinicalNoteWriter
from app.application.ports.llm import ToolCall, ToolDefinition, ToolResult
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IIdGenerator
from app.application.retrieval.observability import RetrievalObserver
from app.domain.auth.value_objects import ResourceType, UserId
from app.domain.shared.errors import ApprovalRequiredError, InvariantViolationError


@dataclass(frozen=True, slots=True)
class _ToolScope:
    actor_id: UserId
    workflow_id: UUID
    owner: ToolOwner


def _error_code(error: Exception) -> ToolErrorCode:
    if isinstance(error, AuthenticationError):
        return ToolErrorCode.NOT_AUTHENTICATED
    if isinstance(error, AuthorizationError):
        return ToolErrorCode.PERMISSION_DENIED
    if isinstance(error, ResourceNotFoundError):
        return ToolErrorCode.RESOURCE_NOT_FOUND
    if isinstance(error, ApprovalMismatchError):
        return ToolErrorCode.APPROVAL_MISMATCH
    if isinstance(error, ApprovalRequiredError):
        return ToolErrorCode.APPROVAL_REQUIRED
    if isinstance(error, FinalNoteConflictError):
        return ToolErrorCode.FINAL_NOTE_CONFLICT
    if isinstance(error, UnknownToolError):
        return ToolErrorCode.UNKNOWN_TOOL
    if isinstance(error, ToolOutputError):
        return ToolErrorCode.INVALID_TOOL_OUTPUT
    if isinstance(error, InvariantViolationError):
        return ToolErrorCode.INVALID_ARGUMENTS
    if isinstance(error, (ClinicalToolStoreError, KnowledgeUnavailableError, TimeoutError)):
        return ToolErrorCode.TOOL_UNAVAILABLE
    return ToolErrorCode.TOOL_EXECUTION_FAILED


class ClinicalToolExecutor:
    """A capability scoped by the server, not a registry or a user-selectable role.

    Five read/draft tool scopes receive no final-note writer at all. The closed
    allow-list is still checked on EVERY execution, independently of which tool
    definitions were advertised to the model.
    """

    def __init__(
        self,
        scope: _ToolScope,
        users: IUserRepository,
        authorization: AuthorizationService,
        contracts: IClinicalToolContracts,
        read_tools: ReadClinicalTools,
        finalizer: IFinalClinicalNoteWriter | None,
        observer: RetrievalObserver,
        identifiers: IIdGenerator,
    ) -> None:
        self.__scope = scope
        self._users = users
        self._authorization = authorization
        self._contracts = contracts
        self._read = read_tools
        self._finalizer = finalizer
        self._observer = observer
        self._identifiers = identifiers

    def definitions(self) -> list[ToolDefinition]:
        return [
            self._contracts.definition(name)
            for name, policy in TOOL_POLICIES.items()
            if policy.owner == self.__scope.owner
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        trace_id = self._identifiers.new_id()
        with trace_scope(
            UUID(trace_id),
            UUID(self.__scope.actor_id.value),
            run_id=self.__scope.workflow_id,
            kind="tool",
        ):
            return await self._execute(call, trace_id)

    async def _execute(self, call: ToolCall, trace_id: str) -> ToolResult:
        started_at = self._observer.clock.now()
        started = perf_counter()
        principal: Principal | None = None
        name: ToolName | None = None
        outcome = "error"
        telemetry: dict[str, object] = {
            "agent_scope": self.__scope.owner.value,
            "workflow_id": str(self.__scope.workflow_id),
        }
        payload: dict[str, object] = {"trace_id": trace_id}
        try:
            require_text(call.id, "tool call id", 200)
            # The saved principal's role is deliberately not retained in a scope.
            # A deleted account or a changed persisted role takes effect now.
            user = await self._users.get_by_id(self.__scope.actor_id)
            if user is None:
                raise UnknownPrincipalError("Tool actor is no longer present")
            principal = Principal.from_user(user)
            telemetry["actor_role"] = principal.role.value
            try:
                name = ToolName(call.name)
            except (TypeError, ValueError):
                raise UnknownToolError("Only the six clinical tools are available") from None
            telemetry["tool"] = name.value
            policy = TOOL_POLICIES[name]
            if policy.owner != self.__scope.owner:
                raise ToolPermissionError("Tool is outside the server-bound agent scope")
            for permission in policy.permissions:
                self._authorization.require_permission(principal, permission)
            await self._authorization.require_resource_access(
                principal, ResourceType.RUN, str(self.__scope.workflow_id)
            )
            request = self._contracts.decode(name, call.arguments)
            if isinstance(request, FinalizeClinicalNoteInput):
                telemetry.update(approval_id=str(request.approval_id), draft_id=request.draft_id)
            result = await self._dispatch(name, request, principal)
            encoded = self._contracts.encode(name, result)
            payload.update(ok=True, tool=name.value, result=encoded)
            outcome = "completed"
            if not isinstance(result, FinalizeClinicalNoteOutput):
                outcome = "refused" if result.refused else "completed"
                telemetry["refused"] = result.refused
                telemetry["evidence_trace_id"] = str(result.evidence_trace_id)
                telemetry["cited_chunks"] = [
                    {"chunk_id": str(citation.chunk_id), "score": citation.relevance_score}
                    for citation in result.citations
                ]
            if isinstance(request, FinalizeClinicalNoteInput):
                telemetry["note_id"] = encoded["note_id"]
        except Exception as exc:
            code = _error_code(exc)
            telemetry["error_code"] = code.value
            outcome = (
                "denied"
                if code
                in {
                    ToolErrorCode.NOT_AUTHENTICATED,
                    ToolErrorCode.PERMISSION_DENIED,
                    ToolErrorCode.APPROVAL_REQUIRED,
                    ToolErrorCode.APPROVAL_MISMATCH,
                    ToolErrorCode.UNKNOWN_TOOL,
                }
                else "error"
            )
            payload.update(
                ok=False,
                tool=name.value if name else None,
                error={"code": code.value, "message": ERROR_MESSAGES[code]},
            )
        finally:
            telemetry["latency_ms"] = (perf_counter() - started) * 1000
            # Do not log argument bodies, draft text, or approved text. The existing
            # #10 spans retain their normal evidence telemetry; link them by ID.
            await self._observer.sink.record(
                AuditEntry(
                    actor_id=self.__scope.actor_id.value,
                    actor_role=principal.role.value if principal else "unknown",
                    action="clinical_tool." + (name.value if name else "unknown"),
                    outcome=outcome,
                    occurred_at=self._observer.clock.now(),
                    resource_type="trace",
                    resource_id=trace_id,
                    correlation_id=get_current_correlation_id() or trace_id,
                    detail={
                        "query": "",
                        "started_at": started_at.isoformat(),
                        "telemetry": json.dumps(telemetry, allow_nan=False),
                    },
                )
            )
        return ToolResult(tool_call_id=call.id, output=json.dumps(payload, allow_nan=False))

    async def _dispatch(
        self, name: ToolName, request: ToolInput, principal: Principal
    ) -> ToolOutput:
        if name == ToolName.SEARCH_CORPUS and isinstance(request, SearchCorpusInput):
            return await self._read.search_corpus(request, principal)
        if name == ToolName.RETRIEVE_DRUG_INFO and isinstance(request, RetrieveDrugInfoInput):
            return await self._read.retrieve_drug_info(request, principal)
        if name == ToolName.CHECK_INTERACTIONS and isinstance(request, CheckInteractionsInput):
            return await self._read.check_interactions(request, principal)
        if name == ToolName.VALIDATE_DOSAGE and isinstance(request, ValidateDosageInput):
            return await self._read.validate_dosage(request, principal)
        if name == ToolName.DRAFT_CLINICAL_NOTE and isinstance(request, DraftClinicalNoteInput):
            return await self._read.draft_clinical_note(
                request, principal, self.__scope.workflow_id
            )
        if name == ToolName.FINALIZE_CLINICAL_NOTE and isinstance(
            request, FinalizeClinicalNoteInput
        ):
            if request.workflow_id != self.__scope.workflow_id:
                raise ApprovalMismatchError("The request is for a different workflow scope")
            if self._finalizer is None:
                raise ToolPermissionError("This scope has no final-note write capability")
            return await self._finalizer.finalize(request, principal.user_id)
        raise ToolInputError("Arguments do not match the requested tool")


class ClinicalToolFactory:
    """Trusted server wiring for future #14-#17 callers; never expose this to a model.

    Obtain principal through #5 authentication (or reload the authenticated job
    owner). Choose one of the four explicit factory methods in server code, not
    from JSON, headers, prompt text, or a model-supplied agent name.
    """

    def __init__(
        self,
        users: IUserRepository,
        authorization: AuthorizationService,
        contracts: IClinicalToolContracts,
        read_tools: ReadClinicalTools,
        finalizer: IFinalClinicalNoteWriter,
        observer: RetrievalObserver,
        identifiers: IIdGenerator,
    ) -> None:
        self._users = users
        self._authorization = authorization
        self._contracts = contracts
        self._read = read_tools
        self._finalizer = finalizer
        self._observer = observer
        self._identifiers = identifiers

    def for_guideline_researcher(
        self, principal: Principal, workflow_id: UUID
    ) -> ClinicalToolExecutor:
        return self._bind(principal, workflow_id, ToolOwner.GUIDELINE_RESEARCHER)

    def for_safety_checker(self, principal: Principal, workflow_id: UUID) -> ClinicalToolExecutor:
        return self._bind(principal, workflow_id, ToolOwner.SAFETY_CHECKER)

    def for_documentation_drafter(
        self, principal: Principal, workflow_id: UUID
    ) -> ClinicalToolExecutor:
        return self._bind(principal, workflow_id, ToolOwner.DOCUMENTATION_DRAFTER)

    def for_orchestrator(self, principal: Principal, workflow_id: UUID) -> ClinicalToolExecutor:
        return self._bind(principal, workflow_id, ToolOwner.ORCHESTRATOR)

    def _bind(
        self, principal: Principal, workflow_id: UUID, owner: ToolOwner
    ) -> ClinicalToolExecutor:
        if not isinstance(principal, Principal):
            raise UnknownPrincipalError("A server-authenticated principal is required")
        require_uuid(workflow_id, "workflow_id")
        return ClinicalToolExecutor(
            _ToolScope(principal.user_id, workflow_id, owner),
            self._users,
            self._authorization,
            self._contracts,
            self._read,
            self._finalizer if owner == ToolOwner.ORCHESTRATOR else None,
            self._observer,
            self._identifiers,
        )
