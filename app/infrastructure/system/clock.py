"""System clock adapter (implements :class:`IClock`)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.application.ports.system import IClock


class SystemClock(IClock):
    """Returns the real wall-clock time as timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)
