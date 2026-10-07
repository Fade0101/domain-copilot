"""System ports: time and identity.

Abstracting "what time is it" and "give me a new id" behind ports keeps use
cases deterministic and unit-testable (tests inject a fixed clock and a
sequential id generator) and keeps ``datetime.now()``/``uuid4()`` side effects
out of the domain and application layers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class IClock(Protocol):
    """Source of the current time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC ``datetime``."""
        ...


class IIdGenerator(Protocol):
    """Source of new unique identifiers (UUID strings)."""

    def new_id(self) -> str:
        """Return a new globally-unique id as a UUID string."""
        ...
