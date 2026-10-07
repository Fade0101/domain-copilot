"""In-memory ownership query adapter (BRD AC-8.4, SEC-1a).

Stands in for the ``SELECT user_id FROM <table> WHERE id = :id`` that the SQL
adapter will issue once Ticket #6's models land. The shape of the answer is what
matters and it is the same either way: an owner, or nothing.

:meth:`register` is not part of :class:`IOwnershipQuery`. The port is read-only
because authorization only ever reads; recording ownership is the job of whichever
feature creates the run, job, trace, or session. Until those features exist this
adapter-specific method is how tests and seeding populate the store, which is why
the container exposes this concrete type rather than only the port.
"""

from __future__ import annotations

from app.application.ports.ownership import IOwnershipQuery
from app.domain.auth.value_objects import ResourceType, UserId


class InMemoryOwnershipQuery(IOwnershipQuery):
    """Dict-backed implementation of the ownership query port."""

    def __init__(self) -> None:
        self._owners: dict[tuple[str, str], str] = {}

    def register(self, resource_type: ResourceType, resource_id: str, owner_id: UserId) -> None:
        """Record that ``owner_id`` owns ``resource_id`` (adapter-specific; see module docs)."""
        self._owners[(resource_type.value, resource_id)] = owner_id.value

    async def owner_of(self, resource_type: ResourceType, resource_id: str) -> UserId | None:
        owner = self._owners.get((resource_type.value, resource_id))
        if owner is None:
            return None
        return UserId(owner)
