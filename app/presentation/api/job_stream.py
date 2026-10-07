"""SSE framing and connection-local polling; never starts or cancels jobs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request

from app.application.auth.context import Principal
from app.application.errors import AuthorizationError, JobStoreError, UnknownPrincipalError
from app.application.jobs.service import JobService
from app.domain.jobs.events import JobEvent, JobEventPage
from app.domain.shared.errors import InvariantViolationError

POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 15.0
PAGE_SIZE = 100


def last_sequence(header: str | None) -> int:
    if header is None or header == "":
        return 0
    if not header.isascii() or not header.isdecimal() or len(header) > 10:
        raise InvariantViolationError("Last-Event-ID must be a non-negative sequence number.")
    sequence = int(header)
    if sequence > 2_147_483_647:
        raise InvariantViolationError("Last-Event-ID exceeds the job event sequence range.")
    return sequence


def encode_event(event: JobEvent) -> str:
    data = json.dumps(
        {"job_id": str(event.job_id), "created_at": event.created_at.isoformat(), **event.payload},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return f"id: {event.sequence_number}\nevent: {event.event_type.value}\ndata: {data}\n\n"


async def stream_events(
    request: Request,
    service: JobService,
    job_id: UUID,
    principal: Principal,
    sequence: int,
    page: JobEventPage,
) -> AsyncIterator[str]:
    heartbeat = monotonic()
    yield ": connected\n\n"
    while not await request.is_disconnected():
        for event in page.events:
            if await request.is_disconnected():
                return
            yield encode_event(event)
            sequence = event.sequence_number
            heartbeat = monotonic()
        if page.job.terminal and len(page.events) < PAGE_SIZE:
            return
        if len(page.events) < PAGE_SIZE:
            await asyncio.sleep(POLL_SECONDS)
            if monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                heartbeat = monotonic()
        try:
            # Recheck the stored role and ownership on every page, including
            # long-lived connections. Reconnect always reads PostgreSQL again.
            page = await service.events_after(job_id, sequence, principal, limit=PAGE_SIZE)
        except (AuthorizationError, UnknownPrincipalError, JobStoreError):
            # Headers are already sent. Close without a fabricated terminal
            # event; reconnect resumes at the last durably delivered sequence.
            return
