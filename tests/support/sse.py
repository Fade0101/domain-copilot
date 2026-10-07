"""Exercise real ASGI streaming/disconnect without httpx buffering the body."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Scope


@dataclass(frozen=True)
class Frame:
    sequence: int
    event: str
    data: dict[str, Any]


def decode_sse(body: str) -> list[Frame]:
    frames = []
    for block in body.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        frames.append(Frame(int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return frames


class LiveStream:
    def __init__(
        self,
        app: ASGIApp,
        path: str,
        headers: dict[str, str],
        on_event: Callable[[Frame], Awaitable[None]] | None = None,
    ) -> None:
        self.disconnected = asyncio.Event()
        self.updated = asyncio.Event()
        self.frames: list[Frame] = []
        self.status: int | None = None
        self._body = b""
        self._request_sent = False
        self._on_event = on_event
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
        self.task: asyncio.Future[None] = asyncio.ensure_future(app(scope, self.receive, self.send))
        self.task.add_done_callback(lambda _: self.updated.set())

    async def receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self._body += message.get("body", b"")
            while b"\n\n" in self._body:
                block, self._body = self._body.split(b"\n\n", 1)
                for frame in decode_sse(block.decode("utf-8") + "\n\n"):
                    if self._on_event:
                        await self._on_event(frame)
                    self.frames.append(frame)
        self.updated.set()

    async def wait_for(self, predicate: Callable[[list[Frame]], bool]) -> None:
        async with asyncio.timeout(15):
            while True:
                self.updated.clear()
                if predicate(self.frames):
                    return
                if self.task.done():
                    await self.task
                    raise AssertionError(f"Stream ended early: HTTP {self.status}, {self.frames}")
                await self.updated.wait()

    async def close(self) -> None:
        self.disconnected.set()
        await asyncio.wait_for(self.task, 5)


@asynccontextmanager
async def live_stream(
    app: ASGIApp,
    path: str,
    headers: dict[str, str],
    on_event: Callable[[Frame], Awaitable[None]] | None = None,
) -> AsyncIterator[LiveStream]:
    stream = LiveStream(app, path, headers, on_event)
    try:
        yield stream
    finally:
        await stream.close()
