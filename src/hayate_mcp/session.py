"""One MCP session = one running SDK ``Server`` plus its stream pair.

The transport talks to the SDK exclusively through anyio memory streams
(DESIGN §3.2): requests go in, a reader task resolves waiting futures by
JSON-RPC id. Server-initiated requests need the optional GET stream, which
v0.1 does not open, so they are dropped with a debug log.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from typing import Any

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCMessage, JSONRPCResponse

logger = logging.getLogger("hayate_mcp")


class McpSession:
    def __init__(self, server: Any, initialization_options: Any) -> None:
        self.id = secrets.token_hex(16)
        self.last_seen = time.monotonic()
        self._pending: dict[Any, asyncio.Future[JSONRPCMessage]] = {}

        to_server_send, to_server_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](math.inf)
        from_server_send, from_server_recv = anyio.create_memory_object_stream[SessionMessage](
            math.inf
        )
        self._to_server = to_server_send
        self._from_server = from_server_recv
        self._run_task = asyncio.ensure_future(
            server.run(to_server_recv, from_server_send, initialization_options)
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    async def _read_loop(self) -> None:
        try:
            async for item in self._from_server:
                root = item.message.root
                if isinstance(root, JSONRPCResponse | JSONRPCError):
                    future = self._pending.get(root.id)
                    if future is not None and not future.done():
                        future.set_result(item.message)
                else:
                    logger.debug(
                        "dropping server-initiated message (no GET stream in v0.1): %s", root
                    )
        except anyio.EndOfStream:  # pragma: no cover - server shut down
            pass

    async def send_notification(self, message: JSONRPCMessage) -> None:
        await self._to_server.send(SessionMessage(message=message))

    async def request(self, message: JSONRPCMessage, *, timeout: float = 30.0) -> JSONRPCMessage:
        request_id = message.root.id
        future: asyncio.Future[JSONRPCMessage] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._to_server.send(SessionMessage(message=message))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        self._run_task.cancel()
        self._reader_task.cancel()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        await self._to_server.aclose()


class MemorySessionStore:
    """Single-process session registry with idle expiry (DESIGN §4)."""

    def __init__(self, *, idle_ttl: float = 3600.0, max_sessions: int = 256) -> None:
        self.idle_ttl = idle_ttl
        self.max_sessions = max_sessions
        self._sessions: dict[str, McpSession] = {}

    def get(self, session_id: str) -> McpSession | None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    async def add(self, session: McpSession) -> None:
        await self._evict()
        self._sessions[session.id] = session

    async def remove(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close_all(self) -> None:
        """Shut every session down (server shutdown hook, test teardown)."""
        for sid in list(self._sessions):
            await self.remove(sid)

    async def _evict(self) -> None:
        deadline = time.monotonic() - self.idle_ttl
        for sid, session in list(self._sessions.items()):
            if session.last_seen < deadline:
                await self.remove(sid)
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            await self.remove(oldest.id)
