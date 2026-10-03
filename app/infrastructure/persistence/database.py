"""Async database engine and session factory.

The smallest persistence primitive Ticket #5 needs, and deliberately no more.
Ticket #6 owns the ORM: there is no ``Base``, no declarative metadata, and no
mapped classes here. The auth adapters that use this issue explicit statements
against tables the Alembic migrations already define, so nothing in this module
needs to know the schema.

``migrations/env.py`` still reads its URL from ``alembic.ini`` and is untouched;
this is the application's runtime connection, not the migration runner's.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

#: Driver this application connects with. Everything here is async, so a URL
#: naming a sync driver would fail at connection time with an unhelpful error.
_ASYNC_DRIVER = "postgresql+asyncpg"


def normalize_database_url(url: str) -> str:
    """Return ``url`` with an async driver.

    A plain ``postgresql://`` URL is what every tool and container image hands
    out, and pasting one into configuration should not be a startup failure. A URL
    that already names a driver is left alone, so an explicitly chosen one is
    respected.
    """
    stripped = url.strip()
    if stripped.startswith("postgresql://"):
        return stripped.replace("postgresql://", f"{_ASYNC_DRIVER}://", 1)
    if stripped.startswith("postgres://"):
        return stripped.replace("postgres://", f"{_ASYNC_DRIVER}://", 1)
    return stripped


class Database:
    """Owns the engine and hands out sessions.

    One instance per process, built by the composition root. ``pool_pre_ping``
    is on because a pooled connection that died while idle would otherwise fail
    the next authorization check rather than being replaced.
    """

    def __init__(self, url: str, *, echo: bool = False, pooling: bool = True) -> None:
        self._engine: AsyncEngine = create_async_engine(
            normalize_database_url(url),
            echo=echo,
            pool_pre_ping=True,
            hide_parameters=True,
            # Celery invokes asyncio.run per task. Async connections cannot move
            # between those event loops; worker adapters therefore opt out of pooling.
            **({} if pooling else {"poolclass": NullPool}),
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the factory adapters use to open a session per operation."""
        return self._session_factory

    async def dispose(self) -> None:
        """Close every pooled connection. Called on application shutdown."""
        await self._engine.dispose()
