"""Composition root (BRD AR-3).

The single place where concrete adapters are constructed and wired to the ports
that use cases depend on. This module -- and only this module -- is permitted to
import from ``app.infrastructure`` and to know concrete adapter classes.

Presentation asks the container for a *use case*; it never instantiates an
adapter. Swapping an adapter (e.g. in-memory -> SQLAlchemy) is a one-line change
here, which is the whole point of the dependency rules (BRD AR-1).

Note on direction: ``core`` importing ``domain`` + ``application`` +
``infrastructure`` is intentional and correct -- the composition root sits at
the outermost point of the dependency graph. Infrastructure must NOT be made to
depend on ``core`` to "avoid" this import.
"""

from __future__ import annotations

from functools import lru_cache

from app.application.documents.use_cases import RegisterDocumentUseCase
from app.application.ports.repositories import IDocumentRepository
from app.application.ports.system import IClock, IIdGenerator
from app.core.config import Settings, get_settings
from app.infrastructure.persistence.in_memory.document_repository import (
    InMemoryDocumentRepository,
)
from app.infrastructure.system.clock import SystemClock
from app.infrastructure.system.identifiers import UuidGenerator


class Container:
    """Holds process-wide singletons and builds use cases via constructor injection.

    Adapter attributes are annotated with their *port* types, so mypy verifies at
    this wiring site that each concrete adapter structurally satisfies its port.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._document_repository: IDocumentRepository = InMemoryDocumentRepository()
        self._clock: IClock = SystemClock()
        self._id_generator: IIdGenerator = UuidGenerator()

    @property
    def document_repository(self) -> IDocumentRepository:
        return self._document_repository

    def register_document_use_case(self) -> RegisterDocumentUseCase:
        return RegisterDocumentUseCase(
            repository=self._document_repository,
            clock=self._clock,
            id_generator=self._id_generator,
        )


@lru_cache
def get_container() -> Container:
    """Return the process-wide :class:`Container` singleton."""
    return Container(settings=get_settings())
