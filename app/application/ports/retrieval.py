"""Port: vector store for dense retrieval (SDD: PostgreSQL + pgvector).

STUB -- final contract in the vector-store ticket (#9). SDK-free: no pgvector or
SQLAlchemy type leaks through this interface, so retrieval logic stays portable.
"""

from __future__ import annotations

from typing import Any, Protocol


class IVectorStore(Protocol):
    async def upsert(self, items: list[Any]) -> None:
        """Persist or replace vector records. Record type finalized in #9."""
        ...

    async def query(self, embedding: list[float], *, top_k: int) -> list[Any]:
        """Return the ``top_k`` nearest records to ``embedding``. Contract finalized in #9."""
        ...
