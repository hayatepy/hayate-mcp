"""McpMount: the Streamable HTTP transport as a pure fetch handler.

Spec: modelcontextprotocol.io, Streamable HTTP transport (2025-06-18 line).
POST carries JSON-RPC and replies with a single JSON body; GET opens the
optional server-initiated SSE stream (one per session); DELETE terminates
a session. Resumability (Last-Event-ID) is out until it can live in the
Durable Object store (DESIGN §4).
"""

from __future__ import annotations

from typing import Any

from hayate import Context, Request, Response, problem
from hayate.sse import event_stream as sse_stream
from mcp.types import JSONRPCMessage, JSONRPCRequest
from pydantic import ValidationError

from .session import McpSession, MemorySessionStore

SESSION_HEADER = "mcp-session-id"


class McpMount:
    def __init__(
        self,
        server: Any,
        *,
        path: str = "/mcp",
        initialization_options: Any | None = None,
        trusted_origins: tuple[str, ...] | list[str] = (),
        store: MemorySessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        self.server = server
        self.path = path.rstrip("/") or "/"
        self.initialization_options = (
            initialization_options
            if initialization_options is not None
            else server.create_initialization_options()
        )
        self.trusted_origins = frozenset(trusted_origins)
        self.store = store if store is not None else MemorySessionStore()
        # When this mount lives inside a per-session Durable Object, its
        # identity is the DO's name: pin it so ``initialize`` returns that id
        # and every later request routes back to the same object (DESIGN §4).
        self.session_id = session_id

    # -- the core ----------------------------------------------------------------------

    async def fetch(self, request: Request) -> Response:
        raw = getattr(request, "raw", request)
        if raw.url.pathname != self.path:
            return problem(404, title="Not Found")

        if not self._origin_allowed(raw):
            return problem(403, title="Origin not allowed")

        if raw.method == "POST":
            return await self._post(raw)
        if raw.method == "DELETE":
            return await self._delete(raw)
        if raw.method == "GET":
            return self._get(raw)
        return problem(405, title="Method Not Allowed", headers={"allow": "GET, POST, DELETE"})

    # -- verbs -------------------------------------------------------------------------

    async def _post(self, raw: Request) -> Response:
        try:
            body = await raw.json()
        except Exception:
            return problem(400, title="Body must be JSON")
        try:
            message = JSONRPCMessage.model_validate(body)
        except ValidationError:
            # 2025-06-18 dropped JSON-RPC batching, so an array is invalid too.
            return problem(400, title="Body must be a single JSON-RPC message")

        is_initialize = (
            isinstance(message.root, JSONRPCRequest) and message.root.method == "initialize"
        )
        if is_initialize:
            session = McpSession(self.server, self.initialization_options, id=self.session_id)
            await self.store.add(session)
        else:
            session_id = raw.headers.get(SESSION_HEADER)
            if session_id is None:
                return problem(400, title=f"Missing {SESSION_HEADER} header")
            session = self.store.get(session_id)
            if session is None:
                return problem(404, title="Session not found")

        if isinstance(message.root, JSONRPCRequest):
            reply = await session.request(message)
            headers = {"content-type": "application/json"}
            if is_initialize:
                headers[SESSION_HEADER] = session.id
            return Response(
                reply.model_dump_json(by_alias=True, exclude_none=True),
                status=200,
                headers=headers,
            )

        # Notifications (and client-side responses) get no reply body.
        await session.send_notification(message)
        return Response(None, status=202)

    def _get(self, raw: Request) -> Response:
        """The optional server-initiated SSE stream (one per session).

        Resumability (Last-Event-ID) is deliberately not implemented in
        v0.2: replay buffers belong with the Durable Object store, where
        sessions survive isolate recycling (DESIGN §4).
        """
        session_id = raw.headers.get(SESSION_HEADER)
        if session_id is None:
            return problem(400, title=f"Missing {SESSION_HEADER} header")
        session = self.store.get(session_id)
        if session is None:
            return problem(404, title="Session not found")
        if not session.claim_stream():
            return problem(409, title="A stream is already open for this session")
        return Response(
            sse_stream(session.outbound_events()),
            status=200,
            headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
        )

    async def _delete(self, raw: Request) -> Response:
        session_id = raw.headers.get(SESSION_HEADER)
        if session_id is None:
            return problem(400, title=f"Missing {SESSION_HEADER} header")
        if not await self.store.remove(session_id):
            return problem(404, title="Session not found")
        return Response(None, status=200)

    # -- helpers -----------------------------------------------------------------------

    def _origin_allowed(self, raw: Request) -> bool:
        """MCP spec MUST: validate Origin to block DNS-rebinding. Requests
        without an Origin (curl, SDKs) are non-browser and pass."""
        origin = raw.headers.get("origin")
        if origin is None or origin == "null":
            return origin != "null"
        return origin == raw.url.origin or origin in self.trusted_origins

    def register(self, app: Any) -> None:
        """Mount on a hayate app (DESIGN TL;DR: this is the whole sugar)."""

        async def mcp_handler(c: Context) -> Response:
            return await self.fetch(c.req)

        for method in ("GET", "POST", "DELETE"):
            app.on(method, self.path)(mcp_handler)
