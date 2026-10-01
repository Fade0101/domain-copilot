"""Harmless infrastructure probe; domain handlers are delivered by #8/#12/#17."""

from __future__ import annotations

from typing import Any

from app.application.ports.jobs import IJobContext, IJobHandler
from app.domain.shared.errors import InvariantViolationError


class DiagnosticJobHandler(IJobHandler):
    operation_type = "diagnostic"

    def validate(self, payload: dict[str, Any]) -> None:
        if payload:
            raise InvariantViolationError("The diagnostic job accepts an empty payload only.")

    async def run(self, context: IJobContext) -> dict[str, Any]:
        async def probe() -> dict[str, Any]:
            return {"ok": True}

        return await context.step("probe-v1", probe)
