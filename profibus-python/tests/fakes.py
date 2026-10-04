"""Test doubles: an in-process stand-in for the mock's TCP JSON-lines protocol."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
from anyio.abc import SocketAttribute
from anyio.streams.buffered import BufferedByteReceiveStream

#: Handler results with a special meaning.
CLOSE = object()  # close the connection without replying
HANG = object()  # never reply

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


def free_port() -> int:
    """A port nothing listens on (bound once, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def fake_mock_server(handler: Handler) -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
    """Serve one JSON request per connection; yield ``(port, received_requests)``."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    received: list[dict[str, Any]] = []

    async def serve(stream: Any) -> None:
        async with stream:
            try:
                line = await BufferedByteReceiveStream(stream).receive_until(b"\n", 65536)
            except (anyio.IncompleteRead, anyio.EndOfStream):
                return
            request = json.loads(line)
            received.append(request)
            reply = await handler(request)
            if reply is CLOSE:
                return
            if reply is HANG:
                await anyio.sleep(30)
                return
            await stream.send(reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n")

    async with listener:
        async with anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, serve)
            try:
                yield port, received
            finally:
                tg.cancel_scope.cancel()


class FakeBackend:
    """A backend that answers from a dict of canned replies and records calls."""

    name = "fake"

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "bus_communication": "fake"}

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, dict[str, Any]]:
        self.calls.append((op, params, retry_after_send))
        reply = self.replies[op]
        if isinstance(reply, Exception):
            raise reply
        return (reply(params) if callable(reply) else reply), {"backend": self.name}
