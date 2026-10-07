"""Session use cases compose grounded Q&A without changing its evidence boundary."""

from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.errors import ApplicationError, UnknownPrincipalError
from app.application.ports.repositories import IUserRepository
from app.application.ports.sessions import ISessionStore, SessionMessage
from app.application.ports.system import IClock, IIdGenerator
from app.application.qa.use_cases import AskResult, AskUseCase
from app.domain.auth.value_objects import Permission, ResourceType
from app.domain.sessions import ConversationSession
from app.domain.shared.errors import InvariantViolationError


class HistoryStoreUnavailableError(ApplicationError):
    """History cannot be read or committed; never pretend persistence succeeded."""


class SessionService:
    def __init__(
        self,
        store: ISessionStore | None,
        users: IUserRepository,
        authorization: AuthorizationService,
        clock: IClock,
        identifiers: IIdGenerator,
    ) -> None:
        self._store = store
        self._users = users
        self._authorization = authorization
        self._clock = clock
        self._ids = identifiers

    def _storage(self) -> ISessionStore:
        if self._store is None:
            raise HistoryStoreUnavailableError("History storage is unavailable.")
        return self._store

    async def _current(self, principal: Principal) -> Principal:
        user = await self._users.get_by_id(principal.user_id)
        if user is None:
            raise UnknownPrincipalError("Session actor is no longer present.")
        current = Principal.from_user(user)
        self._authorization.require_permission(current, Permission.VIEW_OWN_SESSIONS)
        return current

    @staticmethod
    def _pagination(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise InvariantViolationError("Invalid session pagination.")

    async def create(self, title: str, principal: Principal) -> ConversationSession:
        current = await self._current(principal)
        session = ConversationSession(
            UUID(self._ids.new_id()), UUID(current.user_id.value), title.strip(), self._clock.now()
        )
        await self._storage().add(session)
        return session

    async def list_sessions(
        self, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> list[ConversationSession]:
        current = await self._current(principal)
        self._pagination(limit, offset)
        return await self._storage().list_sessions(UUID(current.user_id.value), limit, offset)

    async def messages(
        self, session_id: UUID, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> list[SessionMessage]:
        current = await self._current(principal)
        await self._authorization.require_resource_access(
            current, ResourceType.SESSION, str(session_id)
        )
        self._pagination(limit, offset)
        return await self._storage().messages(
            session_id, UUID(current.user_id.value), limit, offset
        )

    async def ask(
        self, question: str, principal: Principal, session_id: UUID | None, use_case: AskUseCase
    ) -> AskResult:
        if session_id is None:
            return await use_case.execute(question, principal)
        current = await self._current(principal)
        await self._authorization.require_resource_access(
            current, ResourceType.SESSION, str(session_id)
        )
        store = self._storage()
        result = await use_case.execute(question, current)
        # No history is passed to the model as evidence; no raw provider tokens are persisted.
        current = await self._current(principal)
        await store.append_exchange(
            session_id, UUID(current.user_id.value), question, result, self._clock.now()
        )
        return result
