"""Ownership lookup port (BRD AC-8.4, SEC-1a).

Authorization for a specific object must consult *persisted* ownership -- not the
token, not a client-supplied ``owner_id``, and not the absence of a guessable
UUID. This port is the read-side query that makes that possible, kept separate
from the write-side repositories because an authorization check only ever needs
one field: who owns this row.

Keeping it this narrow also means the eventual SQL adapter can answer with a
single indexed ``SELECT user_id`` per check rather than loading an aggregate.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.auth.value_objects import ResourceType, UserId


class IOwnershipQuery(Protocol):
    """Answers "who owns this object?" for the ownership-checked resource types."""

    async def owner_of(self, resource_type: ResourceType, resource_id: str) -> UserId | None:
        """Return the owner of ``resource_id``, or ``None`` if there is no such object.

        ``None`` deliberately conflates "no such row" with "row has no owner": in
        both cases there is no owner to compare against, and neither may result in
        access being granted. The caller turns ``None`` into a 404 and a
        mismatched owner into a 403.

        ``resource_id`` is accepted as an opaque string so a malformed or random
        identifier is simply a lookup that finds nothing, rather than an exception
        on an authorization path.
        """
        ...
