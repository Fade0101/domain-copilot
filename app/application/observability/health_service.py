"""Bounded, concurrent dependency readiness; no exception text crosses the boundary."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.application.ports.observability import IHealthCheckProvider


class HealthService:
    def __init__(self, provider: IHealthCheckProvider, *, timeout_seconds: float = 3) -> None:
        self._provider = provider
        self._timeout = timeout_seconds

    async def check_readiness(self) -> tuple[bool, dict[str, Any]]:
        async def check(probe: Callable[[], Awaitable[bool]]) -> dict[str, str]:
            try:
                async with asyncio.timeout(self._timeout):
                    return {"status": "ok" if await probe() else "unavailable"}
            except TimeoutError:
                return {"status": "timeout"}
            except Exception:
                return {"status": "unavailable"}

        probes = self._provider.probes()
        results = await asyncio.gather(*(check(probe) for probe in probes.values()))
        dependencies = dict(zip(probes, results, strict=True))
        ready = bool(results) and all(result["status"] == "ok" for result in results)
        return ready, {"status": "ready" if ready else "not_ready", "dependencies": dependencies}
