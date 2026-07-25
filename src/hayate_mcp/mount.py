"""McpMount: the Streamable HTTP transport as a pure fetch handler.

Spec: modelcontextprotocol.io, Streamable HTTP transport, tracking the SDK's
latest revision (2025-11-25 with mcp>=1.28 on CPython). POST carries JSON-RPC
and replies with a single JSON body; GET opens the optional server-initiated
SSE stream (one per session); DELETE terminates a session. The
``MCP-Protocol-Version`` header is validated against the SDK's
``SUPPORTED_PROTOCOL_VERSIONS`` (unsupported -> 400). Resumability
(Last-Event-ID) is out until it can live in the Durable Object store
(DESIGN §4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from hayate import Context, Request, Response, problem
from hayate.sse import event_stream as sse_stream
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import JSONRPCError, JSONRPCMessage, JSONRPCRequest, JSONRPCResponse
from pydantic import ValidationError

from .authorization import Authorization
from .context import request_context
from .origin import origin_allowed
from .principal import Principal, principal_context, principal_identity
from .session import McpSession, MemorySessionStore

SESSION_HEADER = "mcp-session-id"
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"


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
        stateless: bool = False,
        authorization: Authorization | None = None,
        tool_scopes: Mapping[str, Sequence[str]] | None = None,
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
        # Stateless mode (DESIGN §6.1): every request runs the SDK Server to
        # completion on its own — no persistent session, no long-lived task.
        # This is the mode that runs on Cloudflare Workers, where a bounded
        # request cannot host a detached ``server.run`` (research/workers-do.md).
        self.stateless = stateless
        # OAuth 2.0 Resource Server config (DESIGN §5): when set, MCP requests
        # require a valid Bearer token and the RFC 9728 metadata is served.
        self.authorization = authorization
        self.tool_scopes = {
            name: tuple(dict.fromkeys(scopes)) for name, scopes in (tool_scopes or {}).items()
        }

    # -- the core ----------------------------------------------------------------------

    async def fetch(self, request: Request) -> Response:
        raw = getattr(request, "raw", request)

        if self.authorization is not None and raw.url.pathname == self._metadata_path():
            if raw.method != "GET":
                return problem(405, title="Method Not Allowed", headers={"allow": "GET"})
            return self._serve_metadata()

        if raw.url.pathname != self.path:
            return problem(404, title="Not Found")

        if not self._origin_allowed(raw):
            return problem(403, title="Origin not allowed")

        # MCP-Protocol-Version header (transports spec, 2025-06-18+): an
        # unsupported value is a hard 400. GET/DELETE carry no body, so this
        # is the only place they can declare a version; for POST the header
        # is absent on the initialize request and validated afterwards.
        if raw.method in ("GET", "DELETE") and not self._protocol_version_ok(raw):
            return problem(400, title="Unsupported MCP-Protocol-Version")

        principal: Principal | None = None
        if self.authorization is not None:
            authorization_header = raw.headers.get("authorization")
            principal = await self.authorization.authenticate(authorization_header)
            if principal is None:
                error = "invalid_token" if authorization_header is not None else None
                return self._unauthorized(error=error)
            missing = self.authorization.missing_scopes(principal)
            if missing:
                return self._insufficient_scope(self.authorization.required_scopes)

        with principal_context(principal):
            if raw.method == "POST":
                return await self._post(raw, principal)
            if raw.method == "DELETE":
                return await self._delete(raw, principal)
            if raw.method == "GET":
                return self._get(raw, principal)
            return problem(405, title="Method Not Allowed", headers={"allow": "GET, POST, DELETE"})

    def _protocol_version_ok(self, raw: Request) -> bool:
        """True unless the client declared a version this server can't speak.

        A missing header passes (the transports spec says assume 2025-03-26
        for back-compat); a present-but-unsupported value must 400."""
        version = raw.headers.get(PROTOCOL_VERSION_HEADER)
        return version is None or version in SUPPORTED_PROTOCOL_VERSIONS

    # -- verbs -------------------------------------------------------------------------

    async def _post(self, raw: Request, principal: Principal | None) -> Response:
        if not self._accepts(raw, CONTENT_TYPE_JSON, CONTENT_TYPE_SSE):
            return problem(
                406,
                title="Not Acceptable",
                detail="MCP POST requests must accept application/json and text/event-stream",
            )
        if self._media_type(raw.headers.get("content-type")) != CONTENT_TYPE_JSON:
            return problem(
                415,
                title="Unsupported Media Type",
                detail="MCP POST requests must use application/json",
            )
        try:
            body = json.loads(
                (await raw.bytes()).decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError):
            return problem(400, title="Body must be UTF-8 JSON")
        try:
            message = JSONRPCMessage.model_validate(body)
        except ValidationError:
            # 2025-06-18 dropped JSON-RPC batching, so an array is invalid too.
            return problem(400, title="Body must be a single JSON-RPC message")

        is_initialize = (
            isinstance(message.root, JSONRPCRequest) and message.root.method == "initialize"
        )
        # The MCP-Protocol-Version header is sent on every request *after*
        # initialize; validate it there (initialize has no negotiated version
        # yet, so it is exempt).
        if not is_initialize and not self._protocol_version_ok(raw):
            return problem(400, title="Unsupported MCP-Protocol-Version")

        if self.authorization is not None:
            assert principal is not None
            required = self._required_tool_scopes(message)
            missing = self.authorization.missing_scopes(principal, required)
            if missing:
                return self._insufficient_scope(list(required))

        if self.stateless:
            return await self._post_stateless(message)

        owner = principal_identity(principal)
        if is_initialize:
            session = McpSession(
                self.server,
                self.initialization_options,
                id=self.session_id,
                owner=owner,
            )
            await self.store.add(session)
        else:
            session_id = raw.headers.get(SESSION_HEADER)
            if session_id is None:
                return problem(400, title=f"Missing {SESSION_HEADER} header")
            existing = self.store.get(session_id)
            if existing is None:
                return problem(404, title="Session not found")
            if existing.owner != owner:
                return problem(404, title="Session not found")
            session = existing

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

    async def _post_stateless(self, message: JSONRPCMessage) -> Response:
        """Run the SDK Server to completion for this one message.

        A fresh stateless ``ServerSession`` treats itself as already
        initialized, so any request — including ``initialize`` — is handled
        without a persistent session. ``server.run`` returns as soon as the
        request stream closes, so there is no detached task: the whole thing
        fits inside a single bounded request (Workers-safe)."""
        import anyio
        from mcp.shared.message import SessionMessage

        if not isinstance(message.root, JSONRPCRequest):
            # Stateless has nowhere to route a bare notification; accept it.
            return Response(None, status=202)

        to_server_send, to_server_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](1)
        from_server_send, from_server_recv = anyio.create_memory_object_stream[SessionMessage](8)

        reply: JSONRPCMessage | None = None
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._run_server_once, to_server_recv, from_server_send)
            await to_server_send.send(SessionMessage(message=message))
            async for item in from_server_recv:
                root = item.message.root
                if isinstance(root, JSONRPCResponse | JSONRPCError) and root.id == message.root.id:
                    reply = item.message
                    break
            await to_server_send.aclose()

        if reply is None:  # pragma: no cover - server produced no response
            return problem(500, title="No response from MCP server")
        return Response(
            reply.model_dump_json(by_alias=True, exclude_none=True),
            status=200,
            headers={"content-type": "application/json"},
        )

    async def _run_server_once(self, read_stream: Any, write_stream: Any) -> None:
        await self.server.run(
            read_stream, write_stream, self.initialization_options, stateless=True
        )

    def _get(self, raw: Request, principal: Principal | None) -> Response:
        """The optional server-initiated SSE stream (one per session).

        Stateless mode has no persistent session to stream from, so the
        server-initiated stream is not offered there (405). Resumability
        (Last-Event-ID) stays unimplemented until it can live in a durable
        store (DESIGN §4).
        """
        if self.stateless:
            return problem(405, title="Method Not Allowed", headers={"allow": "POST"})
        if not self._accepts(raw, CONTENT_TYPE_SSE):
            return problem(
                406,
                title="Not Acceptable",
                detail="MCP GET requests must accept text/event-stream",
            )
        session_id = raw.headers.get(SESSION_HEADER)
        if session_id is None:
            return problem(400, title=f"Missing {SESSION_HEADER} header")
        session = self.store.get(session_id)
        if session is None:
            return problem(404, title="Session not found")
        if session.owner != principal_identity(principal):
            return problem(404, title="Session not found")
        if not session.claim_stream():
            return problem(409, title="A stream is already open for this session")
        return Response(
            sse_stream(session.outbound_events()),
            status=200,
            headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
        )

    async def _delete(self, raw: Request, principal: Principal | None) -> Response:
        if self.stateless:
            # Nothing to terminate; the client's request is a well-formed no-op.
            return Response(None, status=200)
        session_id = raw.headers.get(SESSION_HEADER)
        if session_id is None:
            return problem(400, title=f"Missing {SESSION_HEADER} header")
        session = self.store.get(session_id)
        if session is None or session.owner != principal_identity(principal):
            return problem(404, title="Session not found")
        if not await self.store.remove(session_id):
            return problem(404, title="Session not found")
        return Response(None, status=200)

    # -- helpers -----------------------------------------------------------------------

    def _origin_allowed(self, raw: Request) -> bool:
        """MCP spec MUST: validate Origin to block DNS-rebinding. Requests
        without an Origin (curl, SDKs) are non-browser and pass."""
        return origin_allowed(
            raw.headers.get("origin"),
            raw.url.origin,
            self.trusted_origins,
        )

    def _metadata_path(self) -> str:
        # RFC 9728 §3.1 path-insertion form, derived from the resource
        # identifier (matches the URL the 401 WWW-Authenticate advertises).
        authorization = self.authorization
        assert authorization is not None
        return authorization.metadata_path

    def _serve_metadata(self) -> Response:
        authorization = self.authorization
        assert authorization is not None
        return Response(
            _json_dumps(authorization.metadata()),
            status=200,
            headers={"content-type": "application/json"},
        )

    @staticmethod
    def _media_type(value: str | None) -> str:
        return (value or "").split(";", 1)[0].strip().lower()

    @staticmethod
    def _accepts(raw: Request, *required: str) -> bool:
        accepted: set[str] = set()
        for item in (raw.headers.get("accept") or "").split(","):
            media_type, *parameters = item.split(";")
            quality = 1.0
            for parameter in parameters:
                name, separator, value = parameter.strip().partition("=")
                if separator and name.lower() == "q":
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0.0
            if 0 < quality <= 1:
                accepted.add(media_type.strip().lower())
        return all(media_type in accepted for media_type in required)

    def _required_tool_scopes(self, message: JSONRPCMessage) -> tuple[str, ...]:
        root = message.root
        if not isinstance(root, JSONRPCRequest) or root.method != "tools/call":
            return ()
        params = root.params
        if not isinstance(params, dict):
            return ()
        name = params.get("name")
        return self.tool_scopes.get(name, ()) if isinstance(name, str) else ()

    def _unauthorized(self, *, error: str | None = None) -> Response:
        authorization = self.authorization
        assert authorization is not None
        res = problem(401, title="Authorization required")
        res.headers.set(
            "www-authenticate",
            authorization.www_authenticate(error, scopes=tuple(authorization.required_scopes)),
        )
        return res

    def _insufficient_scope(self, required: list[str]) -> Response:
        authorization = self.authorization
        assert authorization is not None
        res = problem(
            403,
            title="Insufficient scope",
            extensions={"required_scopes": required},
        )
        res.headers.set(
            "www-authenticate",
            authorization.www_authenticate("insufficient_scope", scopes=tuple(required)),
        )
        return res

    def register(self, app: Any) -> None:
        """Mount on a hayate app (DESIGN TL;DR: this is the whole sugar)."""

        async def mcp_handler(c: Context) -> Response:
            with request_context(c):
                return await self.fetch(c.req.raw)

        for method in ("GET", "POST", "DELETE"):
            app.on(method, self.path)(mcp_handler)

        # RFC 9728: the metadata lives at a fixed well-known path, not under
        # the MCP path, so it needs its own route.
        if self.authorization is not None:
            app.on("GET", self._metadata_path())(mcp_handler)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"))


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")
