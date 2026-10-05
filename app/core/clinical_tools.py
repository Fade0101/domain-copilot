"""Identical #18 tool wiring for application callers and containment evaluation."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.auth.authorization import AuthorizationService
from app.application.clinical_tools.execution import ClinicalToolFactory
from app.application.clinical_tools.read_tools import ReadClinicalTools
from app.application.ports.repositories import IUserRepository
from app.application.ports.system import IClock, IIdGenerator
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase
from app.infrastructure.clinical_tools.contracts import ClinicalToolContracts
from app.infrastructure.persistence.sql.clinical_note_writer import PostgresFinalClinicalNoteWriter


def build_clinical_tool_factory(
    users: IUserRepository,
    authorization: AuthorizationService,
    retrieval: HybridRetrievalUseCase,
    ask: AskUseCase,
    sessions: async_sessionmaker[AsyncSession],
    observer: RetrievalObserver,
    clock: IClock,
    identifiers: IIdGenerator,
) -> ClinicalToolFactory:
    return ClinicalToolFactory(
        users,
        authorization,
        ClinicalToolContracts(),
        ReadClinicalTools(retrieval, ask),
        PostgresFinalClinicalNoteWriter(sessions, authorization, clock),
        observer,
        identifiers,
    )
