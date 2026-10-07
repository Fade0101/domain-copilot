"""Real, non-generating chat-provider probes plus PostgreSQL/Redis and local embeddings."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import httpx
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.embeddings import IEmbeddingProvider


@dataclass(frozen=True)
class ChatProbe:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = field(default="", repr=False)

    async def check(self) -> bool:
        async with httpx.AsyncClient(timeout=2) as client:
            if self.provider == "groq":
                if not self.api_key:
                    return False
                response = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": "Bearer " + self.api_key},
                )
                response.raise_for_status()
                body = response.json()
                # Groq exposes the model list; a detail URL can return 404 even
                # for an available model (including provider/model identifiers).
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    return False
                return any(
                    isinstance(item, dict)
                    and item.get("id") == self.model
                    and item.get("active", True) is True
                    for item in body["data"]
                )
            if self.provider == "ollama":
                response = await client.post(
                    self.base_url.rstrip("/") + "/api/show", json={"model": self.model}
                )
                response.raise_for_status()
                return bool(response.json().get("model_info"))
            return False


class DependencyHealthChecks:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        redis_url: str,
        embeddings: IEmbeddingProvider,
        chat: Mapping[str, ChatProbe],
    ) -> None:
        self._sessions = sessions
        self._redis_url = redis_url
        self._embeddings = embeddings
        self._chat = chat
        self._embedding_probe: asyncio.Task[bool] | None = None

    def probes(self) -> Mapping[str, Callable[[], Awaitable[bool]]]:
        return {
            "postgres": self._database,
            "redis": self._redis,
            "embeddings": self._embedding,
            **{name: probe.check for name, probe in self._chat.items()},
        }

    async def _database(self) -> bool:
        if self._sessions is None:
            return False
        async with self._sessions() as session:
            return await session.scalar(sa.text("SELECT 1")) == 1

    async def _redis(self) -> bool:
        async with Redis.from_url(
            self._redis_url, socket_connect_timeout=2, socket_timeout=2
        ) as client:
            return bool(await client.ping())

    async def _embedding(self) -> bool:
        async def probe() -> bool:
            result = await self._embeddings.generate_embeddings(["readiness probe"])
            return (
                len(result.vectors) == 1
                and result.dimensions > 0
                and len(result.vectors[0]) == result.dimensions
                and all(math.isfinite(value) for value in result.vectors[0])
            )

        # A cold model can load longer than a readiness deadline. Reuse the one
        # in-flight probe instead of queuing unbounded work on its single executor.
        task = self._embedding_probe
        if task is None:
            task = self._embedding_probe = asyncio.create_task(probe())
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._embedding_probe = None

    async def close(self) -> None:
        if self._embedding_probe is not None:
            self._embedding_probe.cancel()
            await asyncio.gather(self._embedding_probe, return_exceptions=True)
            self._embedding_probe = None
