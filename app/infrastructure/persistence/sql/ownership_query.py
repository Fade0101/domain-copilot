"""PostgreSQL ownership query (BRD AC-8.4, SEC-1a).

The durable half of object-level authorization. Every check is one indexed read:

    SELECT user_id FROM <table> WHERE id = :resource_id

Two details carry the security weight:

* **The table name never comes from input.** It is looked up in
  :data:`_TABLE_BY_RESOURCE`, keyed by the :class:`ResourceType` enum, so the only
  reachable values are the five written in this module. The resource id is a bound
  parameter. There is no string concatenation of caller data.
* **A non-UUID id is a miss, not an error.** The ``id`` columns are ``uuid``, so
  handing PostgreSQL ``'not-a-uuid'`` would raise ``InvalidTextRepresentation``
  and surface as a 500 on an authorization path. Parsing first turns a hostile or
  malformed id into "no such row", which is the same refusal any unknown id gets.

There is no ``register`` method here, unlike the in-memory adapter. Recording
ownership belongs to whichever feature creates a run, job, trace or session;
those rows are written by the tickets that own them, and this only ever reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.ownership import IOwnershipQuery
from app.domain.auth.value_objects import ResourceType, UserId

#: Resource type -> the table whose ``user_id`` column records its owner. Every
#: one of these tables and columns is defined by migration ``addc7d39b90f``,
#: except ``sessions`` which ``7f2a1c4b9e03`` adds.
_TABLE_BY_RESOURCE: Mapping[ResourceType, str] = {
    ResourceType.RUN: "workflow_runs",
    ResourceType.JOB: "jobs",
    ResourceType.TRACE: "traces",
    ResourceType.SESSION: "sessions",
    ResourceType.DOCUMENT: "documents",
}


class SqlOwnershipQuery(IOwnershipQuery):
    """Reads persisted ownership from PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def owner_of(self, resource_type: ResourceType, resource_id: str) -> UserId | None:
        try:
            parsed = UUID(resource_id)
        except (ValueError, AttributeError, TypeError):
            # Not an id this schema could hold, so no row can match it.
            return None

        table = _TABLE_BY_RESOURCE[resource_type]
        statement = text(f"SELECT user_id FROM {table} WHERE id = :resource_id")  # noqa: S608

        async with self._session_factory() as session:
            result = await session.execute(statement, {"resource_id": parsed})
            owner = result.scalar_one_or_none()

        if owner is None:
            return None
        return UserId(str(owner))
