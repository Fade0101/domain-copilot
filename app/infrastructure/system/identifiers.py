"""UUID identifier adapter (implements :class:`IIdGenerator`)."""

from __future__ import annotations

import uuid

from app.application.ports.system import IIdGenerator


class UuidGenerator(IIdGenerator):
    """Generates random UUID4 identifiers as strings."""

    def new_id(self) -> str:
        return str(uuid.uuid4())
