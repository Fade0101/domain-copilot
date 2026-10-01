"""Only explicitly registered handlers can be submitted or run."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.application.ports.jobs import IJobHandler
from app.domain.shared.errors import InvariantViolationError


def validate_json(data: dict[str, Any], limit: int) -> None:
    if not isinstance(data, dict):
        raise InvariantViolationError("Job data must be a JSON object.")
    try:
        encoded = json.dumps(data, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvariantViolationError("Job data must contain valid, finite JSON values.") from exc
    if len(encoded) > limit:
        raise InvariantViolationError(f"Job data must contain at most {limit} bytes.")
    # json.dumps accepts tuples and non-string keys, but PostgreSQL reads them
    # back as different Python types. Keep fresh and resumed step results equal.
    pending: list[Any] = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise InvariantViolationError("Job JSON object keys must be strings.")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif value is not None and type(value) not in {str, bool, int, float}:
            raise InvariantViolationError("Job data must contain only JSON values.")


class JobHandlerRegistry:
    def __init__(self, handlers: Iterable[IJobHandler]) -> None:
        self._handlers: dict[str, IJobHandler] = {}
        for handler in handlers:
            if not handler.operation_type or handler.operation_type in self._handlers:
                raise ValueError("Job handler names must be non-empty and unique.")
            self._handlers[handler.operation_type] = handler

    def get(self, operation_type: str) -> IJobHandler:
        try:
            return self._handlers[operation_type]
        except KeyError as exc:
            raise InvariantViolationError("Unknown job operation type.") from exc
