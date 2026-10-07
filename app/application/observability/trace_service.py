"""Authorized trace queries, using fresh persisted actors and existing RBAC."""

from dataclasses import replace
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import (
    ResourceNotFoundError,
    ResourceOwnershipError,
    UnknownPrincipalError,
)
from app.application.ports.observability import ITraceStore
from app.application.ports.repositories import IUserRepository
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.observability.entities import Span, Trace, TraceQuery
from app.domain.shared.errors import InvariantViolationError


class TraceService:
    def __init__(
        self, store: ITraceStore, users: IUserRepository, authorization: AuthorizationService
    ) -> None:
        self.store = store
        self._users = users
        self._authorization = authorization

    async def _current_actor(self, principal: Principal) -> Principal:
        user = await self._users.get_by_id(principal.user_id)
        if user is None:
            raise UnknownPrincipalError("Trace actor is no longer present.")
        return Principal.from_user(user)

    async def scoped_query(
        self, principal: Principal, query: TraceQuery, *, costs: bool = False
    ) -> TraceQuery:
        current = await self._current_actor(principal)
        if not 1 <= query.limit <= 200 or query.offset < 0:
            raise InvariantViolationError("Invalid trace pagination.")
        for bound in (query.start_time, query.end_time):
            if bound is not None and bound.utcoffset() is None:
                raise InvariantViolationError("Trace time filters must include a timezone.")
        if query.start_time and query.end_time and query.start_time > query.end_time:
            raise InvariantViolationError("start_time must not follow end_time.")
        cross_owner = (
            current.has_permission(Permission.VIEW_COST_DASHBOARD)
            if costs
            else current.may_access_any(ResourceType.TRACE)
        )
        if cross_owner:
            return query
        owner = UUID(current.user_id.value)
        if query.user_id is not None and query.user_id != owner:
            raise ResourceOwnershipError("Cannot query another user's traces or usage.")
        return replace(query, user_id=owner)

    async def query(self, principal: Principal, query: TraceQuery) -> list[Trace]:
        return await self.store.query_traces(await self.scoped_query(principal, query))

    async def spans(
        self, principal: Principal, trace_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> tuple[Trace, list[Span]]:
        if not 1 <= limit <= 200 or offset < 0:
            raise InvariantViolationError("Invalid span pagination.")
        current = await self._current_actor(principal)
        await self._authorization.require_resource_access(
            current, ResourceType.TRACE, str(trace_id)
        )
        trace = await self.store.get_trace(trace_id)
        if trace is None:
            raise ResourceNotFoundError("Trace not found.")
        return trace, await self.store.get_spans(trace_id, limit=limit, offset=offset)
