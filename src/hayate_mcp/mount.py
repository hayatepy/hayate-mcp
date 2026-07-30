"""Dual-era MCP Streamable HTTP transport as a pure hayate fetch handler.

The 2026-07-28 path is self-contained and stateless regardless of the legacy
session policy: every POST carries its protocol/client envelope, routing
headers are cross-checked against the body, and no ``Mcp-Session-Id`` is
issued.  Earlier revisions keep their initialize/session/GET/DELETE lifecycle
so deployed clients are not stranded during the transition.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from hayate import Context, Request, Response, problem
from hayate.sse import event_stream as sse_stream
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    jsonrpc_message_adapter,
)
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS
from pydantic import ValidationError

from . import __version__
from .authorization import Authorization
from .context import get_request_context, request_context
from .origin import origin_allowed
from .principal import Principal, principal_context, principal_identity
from .protocol import (
    ERROR_CODE_HTTP_STATUS,
    HEADER_MISMATCH,
    INVALID_REQUEST,
    MCP_PARAM_HEADER_PREFIX,
    MCP_PROTOCOL_VERSION_HEADER,
    METHOD_NOT_FOUND,
    MODERN_PROTOCOL_VERSIONS,
    MODERN_REMOVED_METHODS,
    PARSE_ERROR,
    ProtocolRejection,
    classify_modern_request,
    find_duplicated_routing_header,
    lower_headers,
    require_client_capabilities,
    should_route_modern,
    validate_mcp_param_headers,
)
from .session import McpSession, MemorySessionStore

SESSION_HEADER = "mcp-session-id"
PROTOCOL_VERSION_HEADER = MCP_PROTOCOL_VERSION_HEADER
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"
_MCP_PARAM_LIST_PAGE_CAP = 100
_SSE_PING_INTERVAL = 15.0


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
        tool_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
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
        # This flag controls only the compatibility-era lifecycle. Modern
        # calls are always sessionless; a subscriptions/listen handler may
        # still own a live response stream until that response closes.
        self.stateless = stateless
        # OAuth 2.0 Resource Server config (DESIGN §5): when set, MCP requests
        # require a valid Bearer token and the RFC 9728 metadata is served.
        self.authorization = authorization
        self.tool_scopes = {
            name: tuple(dict.fromkeys(scopes)) for name, scopes in (tool_scopes or {}).items()
        }
        if tool_capabilities is not None and any(
            not isinstance(name, str) or not isinstance(required, Mapping)
            for name, required in tool_capabilities.items()
        ):
            raise TypeError("tool_capabilities must map tool names to capability objects")
        self.tool_capabilities = {
            name: dict(required) for name, required in (tool_capabilities or {}).items()
        }
        try:
            json.dumps(self.tool_capabilities, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("tool_capabilities must be JSON serializable") from exc

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

        declared_version = raw.headers.get(PROTOCOL_VERSION_HEADER)
        if (
            raw.method in ("GET", "DELETE")
            and declared_version is not None
            and declared_version not in HANDSHAKE_PROTOCOL_VERSIONS
        ):
            return problem(405, title="Method Not Allowed", headers={"allow": "POST"})

        # A stateless request has no session whose negotiated revision can be
        # consulted. Stateful GET/DELETE are checked only after resolving the
        # session and its owner, alongside POST.
        if (
            self.stateless
            and raw.method in ("GET", "DELETE")
            and not self._supported_protocol_version(raw)
        ):
            return problem(400, title="Unsupported MCP-Protocol-Version")

        principal: Principal | None = None
        if self.authorization is not None:
            authorization_header = raw.headers.get("authorization")
            principal = await self.authorization.authenticate_request(raw)
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

    def _supported_protocol_version(self, raw: Request) -> bool:
        """True unless a stateless client declares an unsupported revision.

        A missing header passes (the transports spec says assume 2025-03-26
        for back-compat); a present-but-unsupported value must 400."""
        version = raw.headers.get(PROTOCOL_VERSION_HEADER)
        return version is None or version in HANDSHAKE_PROTOCOL_VERSIONS

    @staticmethod
    def _session_protocol_version_ok(raw: Request, session: McpSession) -> bool:
        """Validate against the one revision negotiated for this session.

        When the header is absent, the session itself is the transport's
        other way to identify the negotiated version, as allowed by the
        backwards-compatibility rule in the Streamable HTTP specification.
        """
        version = raw.headers.get(PROTOCOL_VERSION_HEADER)
        return session.protocol_version is not None and (
            version is None or version == session.protocol_version
        )

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
        transport_headers = lower_headers(raw.headers)
        modern_header = (
            transport_headers.get(MCP_PROTOCOL_VERSION_HEADER) is not None
            and transport_headers[MCP_PROTOCOL_VERSION_HEADER] not in HANDSHAKE_PROTOCOL_VERSIONS
        )
        try:
            body = json.loads(
                (await raw.bytes()).decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError):
            if modern_header:
                return self._protocol_rejection(
                    None,
                    ProtocolRejection(PARSE_ERROR, "Parse error"),
                )
            return problem(400, title="Body must be UTF-8 JSON")
        modern = should_route_modern(body, transport_headers)
        try:
            message = jsonrpc_message_adapter.validate_python(body)
        except ValidationError:
            # 2025-06-18 dropped JSON-RPC batching, so an array is invalid too.
            if modern:
                return self._protocol_rejection(
                    None,
                    ProtocolRejection(
                        INVALID_REQUEST,
                        "Body must be a single JSON-RPC request object",
                    ),
                )
            return problem(400, title="Body must be a single JSON-RPC message")

        if modern:
            if not isinstance(message, JSONRPCRequest) or not isinstance(body, dict):
                return self._protocol_rejection(
                    None,
                    ProtocolRejection(
                        INVALID_REQUEST,
                        "Body must be a single JSON-RPC request object",
                    ),
                )
            duplicated = find_duplicated_routing_header(raw.headers.raw())
            if duplicated is not None:
                return self._protocol_rejection(
                    message.id,
                    ProtocolRejection(
                        HEADER_MISMATCH,
                        f"{duplicated} header appears more than once",
                    ),
                )
            route = classify_modern_request(
                body,
                transport_headers,
                supported_versions=MODERN_PROTOCOL_VERSIONS,
            )
            if isinstance(route, ProtocolRejection):
                return self._protocol_rejection(message.id, route)
            if message.method in MODERN_REMOVED_METHODS:
                return self._protocol_rejection(
                    message.id,
                    ProtocolRejection(METHOD_NOT_FOUND, "Method not found"),
                )
            capability_rejection = self._modern_capability_rejection(
                message,
                route.client_capabilities,
            )
            if capability_rejection is not None:
                return self._protocol_rejection(message.id, capability_rejection)
            if self.authorization is not None:
                assert principal is not None
                required = self._required_tool_scopes(message)
                missing = self.authorization.missing_scopes(principal, required)
                if missing:
                    return self._insufficient_scope(list(required))
            param_rejection = await self._modern_param_rejection(
                message,
                transport_headers,
                raw.headers.raw(),
            )
            if param_rejection is not None:
                return self._protocol_rejection(message.id, param_rejection)
            if message.method == "subscriptions/listen" and self._serves_method(
                "subscriptions/listen"
            ):
                return self._post_modern_stream(message, principal)
            return await self._post_stateless(message, modern=True)

        is_initialize = isinstance(message, JSONRPCRequest) and message.method == "initialize"
        # initialize has no negotiated version yet. A stateless request can
        # only be checked against the SDK's supported set; a stateful request
        # is checked against its exact session version after session lookup.
        if not is_initialize and self.stateless and not self._supported_protocol_version(raw):
            return problem(400, title="Unsupported MCP-Protocol-Version")

        session: McpSession | None = None
        if not self.stateless:
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
                existing = self.store.peek(session_id)
                if existing is None:
                    return problem(404, title="Session not found")
                if existing.owner != owner:
                    return problem(404, title="Session not found")
                session = existing
                if not self._session_protocol_version_ok(raw, session):
                    return problem(400, title="MCP-Protocol-Version does not match session")

        if self.authorization is not None:
            assert principal is not None
            required = self._required_tool_scopes(message)
            missing = self.authorization.missing_scopes(principal, required)
            if missing:
                return self._insufficient_scope(list(required))

        if self.stateless:
            return await self._post_stateless(
                message,
                modern=False,
                legacy_version=raw.headers.get(PROTOCOL_VERSION_HEADER),
            )
        assert session is not None
        if not is_initialize:
            session.touch()

        if isinstance(message, JSONRPCRequest):
            try:
                reply = await session.request(message)
            except BaseException:
                if is_initialize:
                    await self.store.remove(session.id)
                raise
            headers = {"content-type": "application/json"}
            if is_initialize:
                if isinstance(reply, JSONRPCResponse):
                    version = reply.result.get("protocolVersion")
                    if not isinstance(version, str) or not version:
                        await self.store.remove(session.id)
                        return problem(
                            500,
                            title="MCP initialize response did not select a protocol version",
                        )
                    session.bind_protocol_version(version)
                    headers[SESSION_HEADER] = session.id
                else:
                    # A rejected initialize never creates a usable transport
                    # session and therefore must not return a session id.
                    await self.store.remove(session.id)
            return Response(
                reply.model_dump_json(by_alias=True, exclude_none=True),
                status=200,
                headers=headers,
            )

        # Notifications (and client-side responses) get no reply body.
        await session.send_notification(message)
        return Response(None, status=202)

    async def _post_stateless(
        self,
        message: JSONRPCMessage,
        *,
        modern: bool,
        legacy_version: str | None = None,
    ) -> Response:
        """Run the SDK Server to completion for this one message.

        Modern requests are born self-contained.  A pre-2026 stateless call
        that skipped initialize is bootstrapped inside the bounded exchange
        for compatibility with hayate-mcp's established stateless contract.
        """
        if not isinstance(message, JSONRPCRequest):
            return Response(None, status=202)
        outbound: list[JSONRPCMessage] = []
        reply = await self._exchange(
            message,
            bootstrap_legacy=not modern and message.method != "initialize",
            legacy_version=legacy_version,
            outbound=outbound,
        )
        if reply is None:  # pragma: no cover - server produced no response
            return problem(500, title="No response from MCP server")
        status = 200
        if modern and isinstance(reply, JSONRPCError):
            status = ERROR_CODE_HTTP_STATUS.get(reply.error.code, 200)
        if modern and any(item is not reply for item in outbound):
            return Response(
                "".join(
                    f"event: message\ndata: "
                    f"{item.model_dump_json(by_alias=True, exclude_none=True)}\n\n"
                    for item in outbound
                ),
                status=status,
                headers={
                    "content-type": CONTENT_TYPE_SSE,
                    "cache-control": "no-cache, no-transform",
                    "connection": "keep-alive",
                    "x-accel-buffering": "no",
                },
            )
        return Response(
            reply.model_dump_json(by_alias=True, exclude_none=True),
            status=status,
            headers={"content-type": "application/json"},
        )

    async def _exchange(
        self,
        message: JSONRPCRequest,
        *,
        bootstrap_legacy: bool = False,
        legacy_version: str | None = None,
        outbound: list[JSONRPCMessage] | None = None,
    ) -> JSONRPCResponse | JSONRPCError | None:
        """Exchange one request with the SDK's public dual-era server loop."""

        import anyio
        from mcp.shared.message import SessionMessage

        to_server_send, to_server_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](4)
        from_server_send, from_server_recv = anyio.create_memory_object_stream[SessionMessage](16)

        reply: JSONRPCResponse | JSONRPCError | None = None
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._run_server_once, to_server_recv, from_server_send)
            if bootstrap_legacy:
                selected_version = (
                    legacy_version
                    if legacy_version in HANDSHAKE_PROTOCOL_VERSIONS
                    else HANDSHAKE_PROTOCOL_VERSIONS[-1]
                )
                initialize = JSONRPCRequest(
                    jsonrpc="2.0",
                    id="_hayate_stateless_initialize",
                    method="initialize",
                    params={
                        "protocolVersion": selected_version,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "hayate-stateless-bridge",
                            "version": __version__,
                        },
                    },
                )
                await to_server_send.send(SessionMessage(message=initialize))
                async for item in from_server_recv:
                    candidate = item.message
                    if (
                        isinstance(candidate, JSONRPCResponse | JSONRPCError)
                        and candidate.id == initialize.id
                    ):
                        break
                await to_server_send.send(
                    SessionMessage(
                        message=JSONRPCNotification(
                            jsonrpc="2.0",
                            method="notifications/initialized",
                        )
                    )
                )
            await to_server_send.send(SessionMessage(message=message))
            async for item in from_server_recv:
                candidate = item.message
                if outbound is not None:
                    outbound.append(candidate)
                if (
                    isinstance(candidate, JSONRPCResponse | JSONRPCError)
                    and candidate.id == message.id
                ):
                    reply = candidate
                    break
            await to_server_send.aclose()
            tg.cancel_scope.cancel()
        return reply

    def _post_modern_stream(
        self,
        message: JSONRPCRequest,
        principal: Principal | None,
    ) -> Response:
        """Return a response-owned live SSE exchange for subscriptions."""

        return Response(
            self._modern_stream_events(
                message,
                principal,
                get_request_context(),
            ),
            status=200,
            headers={
                "content-type": CONTENT_TYPE_SSE,
                "cache-control": "no-cache, no-transform",
                "connection": "keep-alive",
                "x-accel-buffering": "no",
            },
        )

    async def _modern_stream_events(
        self,
        message: JSONRPCRequest,
        principal: Principal | None,
        context: Context | None,
    ) -> AsyncIterator[bytes]:
        """Drive one live SDK exchange for exactly as long as its response body."""

        import anyio
        from mcp.shared.message import SessionMessage

        to_server_send, to_server_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](1)
        from_server_send, from_server_recv = anyio.create_memory_object_stream[SessionMessage](16)

        try:
            async with anyio.create_task_group() as tg:
                try:
                    # AnyIO copies the active context into the child task.
                    # Reset immediately afterward so yielding response chunks
                    # cannot leak request state into the adapter's send loop.
                    if context is None:
                        with principal_context(principal):
                            tg.start_soon(
                                self._run_server_once,
                                to_server_recv,
                                from_server_send,
                            )
                    else:
                        with principal_context(principal), request_context(context):
                            tg.start_soon(
                                self._run_server_once,
                                to_server_recv,
                                from_server_send,
                            )
                    await to_server_send.send(SessionMessage(message=message))

                    while True:
                        item = None
                        ended = False
                        with anyio.move_on_after(_SSE_PING_INTERVAL) as timeout:
                            try:
                                item = await from_server_recv.receive()
                            except anyio.EndOfStream:
                                ended = True
                        if timeout.cancel_called:
                            yield b": ping\r\n\r\n"
                            continue
                        if ended:
                            return
                        assert item is not None
                        candidate = item.message
                        yield _sse_event(candidate)
                        if (
                            isinstance(candidate, JSONRPCResponse | JSONRPCError)
                            and candidate.id == message.id
                        ):
                            return
                except GeneratorExit:
                    # Response-body close is the modern HTTP cancellation
                    # signal. Consume it here so AnyIO does not wrap it in an
                    # exception group while the server task is being stopped.
                    return
                finally:
                    await to_server_send.aclose()
                    if not tg.cancel_scope.cancel_called:
                        tg.cancel_scope.cancel()
        finally:
            await to_server_send.aclose()
            await from_server_recv.aclose()

    def _serves_method(self, method: str) -> bool:
        getter = getattr(self.server, "get_request_handler", None)
        return callable(getter) and getter(method) is not None

    async def _modern_param_rejection(
        self,
        message: JSONRPCRequest,
        headers: Mapping[str, str],
        raw_headers: Sequence[tuple[str, str]],
    ) -> ProtocolRejection | None:
        """Resolve the caller-visible tool schema and validate routed params."""

        if message.method != "tools/call" or not isinstance(message.params, dict):
            return None
        name = message.params.get("name")
        arguments = message.params.get("arguments", {})
        meta = message.params.get("_meta")
        if (
            not isinstance(name, str)
            or not isinstance(arguments, dict)
            or not isinstance(meta, dict)
        ):
            return None
        if not arguments and not any(
            header.lower().startswith(MCP_PARAM_HEADER_PREFIX.lower())
            for header, _value in raw_headers
        ):
            return None

        seen_cursors: set[str] = set()
        list_params: dict[str, Any] = {"_meta": meta}
        for page in range(_MCP_PARAM_LIST_PAGE_CAP):
            listing = JSONRPCRequest(
                jsonrpc="2.0",
                id=f"_hayate_schema_{message.id}_{page}",
                method="tools/list",
                params=list_params,
            )
            reply = await self._exchange(listing)
            if not isinstance(reply, JSONRPCResponse):
                return None
            tools = reply.result.get("tools")
            if not isinstance(tools, list):
                return None
            for tool in tools:
                if isinstance(tool, dict) and tool.get("name") == name:
                    return validate_mcp_param_headers(
                        tool.get("inputSchema"),
                        arguments,
                        headers,
                        raw_headers=raw_headers,
                    )
            cursor = reply.result.get("nextCursor")
            if not isinstance(cursor, str) or cursor in seen_cursors:
                return None
            seen_cursors.add(cursor)
            list_params = {"_meta": meta, "cursor": cursor}
        return None

    def _modern_capability_rejection(
        self,
        message: JSONRPCRequest,
        declared: Any,
    ) -> ProtocolRejection | None:
        if message.method != "tools/call" or not isinstance(message.params, dict):
            return None
        name = message.params.get("name")
        if not isinstance(name, str):
            return None
        required = self.tool_capabilities.get(name)
        return require_client_capabilities(declared, required) if required is not None else None

    async def _run_server_once(self, read_stream: Any, write_stream: Any) -> None:
        await self.server.run(read_stream, write_stream, self.initialization_options)

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
        session = self.store.peek(session_id)
        if session is None:
            return problem(404, title="Session not found")
        if session.owner != principal_identity(principal):
            return problem(404, title="Session not found")
        if not self._session_protocol_version_ok(raw, session):
            return problem(400, title="MCP-Protocol-Version does not match session")
        if not session.claim_stream():
            return problem(409, title="A stream is already open for this session")
        session.touch()
        return Response(
            sse_stream(session.outbound_events()),
            status=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache, no-transform",
                "connection": "keep-alive",
                "x-accel-buffering": "no",
            },
        )

    async def _delete(self, raw: Request, principal: Principal | None) -> Response:
        if self.stateless:
            # Nothing to terminate; the client's request is a well-formed no-op.
            return Response(None, status=200)
        session_id = raw.headers.get(SESSION_HEADER)
        if session_id is None:
            return problem(400, title=f"Missing {SESSION_HEADER} header")
        session = self.store.peek(session_id)
        if session is None or session.owner != principal_identity(principal):
            return problem(404, title="Session not found")
        if not self._session_protocol_version_ok(raw, session):
            return problem(400, title="MCP-Protocol-Version does not match session")
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
        if not isinstance(message, JSONRPCRequest) or message.method != "tools/call":
            return ()
        params = message.params
        if not isinstance(params, dict):
            return ()
        name = params.get("name")
        return self.tool_scopes.get(name, ()) if isinstance(name, str) else ()

    @staticmethod
    def _protocol_rejection(
        request_id: str | int | None,
        rejection: ProtocolRejection,
    ) -> Response:
        error: dict[str, Any] = {
            "code": rejection.code,
            "message": rejection.message,
        }
        if rejection.data is not None:
            error["data"] = rejection.data
        return Response(
            _json_dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error,
                }
            ),
            status=rejection.http_status,
            headers={"content-type": CONTENT_TYPE_JSON},
        )

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
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def _sse_event(message: JSONRPCMessage) -> bytes:
    data = message.model_dump_json(by_alias=True, exclude_none=True)
    return f"event: message\r\ndata: {data}\r\n\r\n".encode()


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")
