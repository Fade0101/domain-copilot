"""Human review service. It neither runs agents nor invokes finalization."""

from uuid import UUID

from app.application.approvals.contracts import (
    ApprovalCommand,
    DecisionResult,
    DraftReview,
    FinalizationRequest,
    ReviewRecord,
)
from app.application.approvals.rules import decision_permissions, validate_command
from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import AuthorizationError, UnknownPrincipalError
from app.application.ports.approvals import IApprovalStore
from app.application.ports.audit import AuditEntry, IAuditSink
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock
from app.domain.approvals.entities import ApprovalAction, ApprovalDecision
from app.domain.auth.value_objects import Permission, ResourceType


class ApprovalService:
    def __init__(
        self,
        store: IApprovalStore,
        users: IUserRepository,
        authorization: AuthorizationService,
        audit: IAuditSink,
        clock: IClock,
    ) -> None:
        self._store = store
        self._users = users
        self._authorization = authorization
        self._audit = audit
        self._clock = clock

    async def _authorize(
        self, principal: Principal, workflow_id: UUID, permissions: tuple[Permission, ...]
    ) -> Principal:
        if not isinstance(principal, Principal):
            raise UnknownPrincipalError("An authenticated principal is required")
        user = await self._users.get_by_id(principal.user_id)
        if user is None:
            raise UnknownPrincipalError("Review actor is no longer present")
        current = Principal.from_user(user)
        for permission in permissions:
            self._authorization.require_permission(current, permission)
        await self._authorization.require_resource_access(
            current, ResourceType.RUN, str(workflow_id)
        )
        return current

    async def prepare_review(
        self, snapshot: DraftReview, job_id: UUID, principal: Principal
    ) -> ReviewRecord:
        """Internal #17 handoff; intentionally has no HTTP registration endpoint."""
        current = await self._authorize(
            principal, snapshot.draft.workflow_id, (Permission.RUN_WORKFLOW,)
        )
        return await self._store.prepare_review(snapshot, job_id, current.user_id)

    async def get_review(self, workflow_id: UUID, principal: Principal) -> ReviewRecord:
        current = await self._authorize(
            principal, workflow_id, (Permission.VIEW_PENDING_APPROVALS,)
        )
        return await self._store.get(workflow_id, current.user_id)

    async def get_decision(
        self, workflow_id: UUID, principal: Principal
    ) -> ApprovalDecision | None:
        """Authoritative committed decision for an authorized workflow caller."""
        current = await self._authorize(principal, workflow_id, (Permission.RUN_WORKFLOW,))
        return (await self._store.get(workflow_id, current.user_id)).decision

    async def get_finalization_request(
        self, workflow_id: UUID, principal: Principal
    ) -> FinalizationRequest | None:
        """Read the durable signal; #17 owns consuming it and resuming the job."""
        current = await self._authorize(principal, workflow_id, (Permission.RUN_WORKFLOW,))
        return (await self._store.get(workflow_id, current.user_id)).finalization_request

    async def approve(
        self, workflow_id: UUID, draft_id: str, principal: Principal
    ) -> DecisionResult:
        return await self._decide(
            ApprovalCommand(workflow_id, draft_id, ApprovalAction.APPROVE), principal
        )

    async def reject(
        self, workflow_id: UUID, draft_id: str, reason: str, principal: Principal
    ) -> DecisionResult:
        return await self._decide(
            ApprovalCommand(workflow_id, draft_id, ApprovalAction.REJECT, reason=reason), principal
        )

    async def edit_and_approve(
        self, workflow_id: UUID, draft_id: str, edited_note: str, principal: Principal
    ) -> DecisionResult:
        return await self._decide(
            ApprovalCommand(
                workflow_id, draft_id, ApprovalAction.EDIT_AND_APPROVE, edited_note=edited_note
            ),
            principal,
        )

    async def _decide(self, command: ApprovalCommand, principal: Principal) -> DecisionResult:
        current = await self._authorize(
            principal, command.workflow_id, decision_permissions(command.action)
        )
        validate_command(command)
        try:
            result = await self._store.decide(command, current.user_id)
        except AuthorizationError:
            await self._audit.record(
                AuditEntry(
                    actor_id=current.user_id.value,
                    actor_role=current.role.value,
                    action="approval." + command.action.value.lower(),
                    outcome="denied",
                    occurred_at=self._clock.now(),
                    resource_type="run",
                    resource_id=str(command.workflow_id),
                )
            )
            raise
        decision = result.review.decision
        assert decision is not None
        # The durable audit and signal are ALREADY committed by the store. The
        # existing sink receives identifiers only, never note/reason/diff text.
        await self._audit.record(
            AuditEntry(
                actor_id=str(decision.actor_id),
                actor_role=decision.actor_role.value,
                action="approval." + decision.action.value.lower(),
                outcome="replayed" if result.replayed else decision.status.value,
                occurred_at=decision.created_at,
                resource_type="run",
                resource_id=str(decision.workflow_id),
                correlation_id=str(result.review.job_id),
                detail={"approval_id": str(decision.id), "draft_id": decision.draft_id},
            )
        )
        return result
