"""MCP 2025-11-25 tools runtime for Cloudflare Python Workers.

The official Python SDK cannot currently resolve on Cloudflare's Pyodide
runtime because its Pydantic floor needs a newer ``pydantic-core`` wasm
wheel.  This module implements the deliberately small capability surface
advertised by a stateless Workers server: lifecycle, ping, and tools.
Optional capabilities (resources, prompts, logging, sampling, tasks, and
server-initiated streams) are not advertised and therefore are not part of
the negotiated contract.

Wire messages and tool schemas are validated without Pydantic.  JSON Schema
validation uses ``jsonschema`` lazily inside the first request so importing a
Worker module at global scope remains workerd entropy-safe.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hayate import Context, Request, Response, problem

from .authorization import Authorization
from .context import request_context
from .origin import origin_allowed
from .principal import Principal, principal_context

PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MISSING = object()

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]

logger = logging.getLogger("hayate_mcp.worker")


class ToolError(Exception):
    """An expected, model-visible tool failure."""


class _ProtocolError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        data: Any = _MISSING,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.status = status
        self.headers = dict(headers or {})
        super().__init__(message)


class WorkerProtocolError(_ProtocolError):
    """A deliberate JSON-RPC error raised by a Workers tool handler.

    ``status`` and ``headers`` let an application preserve HTTP authentication,
    throttling, and dependency-unavailable semantics around a valid MCP error.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        data: Any = _MISSING,
    ) -> None:
        if not isinstance(code, int) or isinstance(code, bool):
            raise TypeError("protocol error code must be an integer")
        if not isinstance(message, str):
            raise TypeError("protocol error message must be a string")
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or (status != 200 and not 400 <= status <= 599)
        ):
            raise ValueError("protocol error status must be 200 or between 400 and 599")
        if headers is not None and (
            not isinstance(headers, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise TypeError("protocol error headers must map strings to strings")
        if headers is not None and any(
            "\r" in item or "\n" in item for pair in headers.items() for item in pair
        ):
            raise ValueError("protocol error headers must not contain newlines")
        if data is not _MISSING:
            try:
                json.dumps(data, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError("protocol error data must be JSON serializable") from exc
        super().__init__(code, message, data, status=status, headers=headers)


@dataclass(frozen=True)
class WorkerTool:
    """A 2025-11-25 ``Tool`` definition and its local handler."""

    name: str
    handler: ToolHandler = field(repr=False, compare=False)
    input_schema: dict[str, Any]
    description: str | None = None
    title: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    icons: tuple[dict[str, Any], ...] = ()
    meta: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(
                "tool name must be 1-128 ASCII letters, digits, underscores, hyphens, or dots"
            )
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema must have type='object'")
        if self.output_schema is not None and (
            not isinstance(self.output_schema, dict) or self.output_schema.get("type") != "object"
        ):
            raise ValueError("tool output_schema must have type='object'")
        for label, value in (("description", self.description), ("title", self.title)):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"tool {label} must be a string")
        if self.annotations is not None and not _tool_annotations_valid(self.annotations):
            raise TypeError("tool annotations do not match MCP ToolAnnotations")
        if not all(_icon_valid(icon) for icon in self.icons):
            raise TypeError("tool icons do not match MCP Icon")
        if self.meta is not None and not isinstance(self.meta, dict):
            raise TypeError("tool meta must be an object")
        if self.execution is not None and (
            not isinstance(self.execution, dict)
            or set(self.execution) - {"taskSupport"}
            or self.execution.get("taskSupport", "forbidden") != "forbidden"
        ):
            raise TypeError("Workers tools only support execution.taskSupport='forbidden'")
        try:
            json.dumps(self.wire(), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("tool definition must be JSON serializable") from exc

    def wire(self) -> dict[str, Any]:
        value: dict[str, Any] = {"name": self.name, "inputSchema": self.input_schema}
        if self.description is not None:
            value["description"] = self.description
        if self.title is not None:
            value["title"] = self.title
        if self.output_schema is not None:
            value["outputSchema"] = self.output_schema
        if self.annotations is not None:
            value["annotations"] = self.annotations
        if self.icons:
            value["icons"] = list(self.icons)
        if self.meta is not None:
            value["_meta"] = self.meta
        if self.execution is not None:
            value["execution"] = self.execution
        return value


class WorkerMcpServer:
    """Stateless MCP server for the Workers runtime.

    Only the ``tools`` capability is advertised.  Task augmentation is
    intentionally absent: 2025-11-25 clients therefore MUST use ordinary
    ``tools/call``, and requests containing ``params.task`` are rejected.
    """

    def __init__(
        self,
        name: str,
        *,
        version: str,
        title: str | None = None,
        instructions: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("server name must not be empty")
        if not isinstance(version, str) or not version:
            raise ValueError("server version must not be empty")
        if title is not None and not isinstance(title, str):
            raise TypeError("server title must be a string")
        if instructions is not None and not isinstance(instructions, str):
            raise TypeError("server instructions must be a string")
        self.name = name
        self.version = version
        self.title = title
        self.instructions = instructions
        self._tools: dict[str, WorkerTool] = {}
        self._validators: dict[tuple[str, str], Any] | None = None

    def tool(
        self,
        *,
        name: str,
        input_schema: dict[str, Any],
        description: str | None = None,
        title: str | None = None,
        output_schema: dict[str, Any] | None = None,
        annotations: dict[str, Any] | None = None,
        icons: Sequence[dict[str, Any]] = (),
        meta: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Register a tool while keeping the decorated callable unchanged."""

        def register(handler: ToolHandler) -> ToolHandler:
            if name in self._tools:
                raise ValueError(f"tool {name!r} is already registered")
            self._tools[name] = WorkerTool(
                name=name,
                handler=handler,
                input_schema=input_schema,
                description=description,
                title=title,
                output_schema=output_schema,
                annotations=annotations,
                icons=tuple(icons),
                meta=meta,
                execution=execution,
            )
            self._validators = None
            return handler

        return register

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one validated JSON-RPC request and return its result object."""
        await self._ensure_validators()
        method = request["method"]
        params = request.get("params", {})

        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._list_tools(params)
        if method == "tools/call":
            return await self._call_tool(params)
        raise _ProtocolError(-32601, "Method not found")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client_info = params.get("clientInfo")
        if (
            not isinstance(version, str)
            or not isinstance(capabilities, dict)
            or not _implementation_valid(client_info)
        ):
            raise _ProtocolError(-32602, "Invalid initialize parameters")

        # A server that does not support the client's proposed revision MUST
        # answer with one it does support; the client then decides whether to
        # continue (MCP lifecycle version negotiation).
        server_info: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title is not None:
            server_info["title"] = self.title
        result: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info,
        }
        if self.instructions is not None:
            result["instructions"] = self.instructions
        return result

    def _list_tools(self, params: dict[str, Any]) -> dict[str, Any]:
        if "cursor" in params:
            # This server never emits a cursor, so no cursor can be one
            # previously issued by it.  MCP cursors are strings, not null.
            raise _ProtocolError(-32602, "Invalid cursor")
        return {"tools": [tool.wire() for tool in self._tools.values()]}

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        if "task" in params:
            # Tasks are experimental and not advertised.  The tasks spec
            # requires Method not found for augmentation of a forbidden tool.
            raise _ProtocolError(-32601, "Task-augmented tools/call is not supported")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise _ProtocolError(-32602, "Invalid tools/call parameters")
        tool = self._tools.get(name)
        if tool is None:
            raise _ProtocolError(-32602, f"Unknown tool: {name}")

        validator = self._validator(name, "input")
        errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
        if errors:
            messages = "; ".join(error.message for error in errors[:8])
            return _text_result(
                f"Input validation error: {messages}",
                is_error=True,
            )

        try:
            outcome = tool.handler(arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            result = _normalize_tool_result(outcome)
        except WorkerProtocolError:
            raise
        except ToolError as exc:
            result = _text_result(str(exc), is_error=True)
        except Exception:
            logger.exception("tool %s failed", name)
            result = _text_result("Tool execution failed", is_error=True)

        if tool.output_schema is not None and not result.get("isError", False):
            structured = result.get("structuredContent", _MISSING)
            if not isinstance(structured, dict):
                logger.error("tool %s omitted structuredContent required by outputSchema", name)
                return _text_result("Tool returned an invalid structured result", is_error=True)
            output_errors = sorted(
                self._validator(name, "output").iter_errors(structured),
                key=lambda error: list(error.path),
            )
            if output_errors:
                logger.error("tool %s returned output that does not match outputSchema", name)
                return _text_result("Tool returned an invalid structured result", is_error=True)
        return result

    async def _ensure_validators(self) -> None:
        if self._validators is not None:
            return
        # Imported in request scope: rpds may request entropy during import,
        # which workerd deliberately forbids while evaluating global modules.
        from jsonschema.validators import validator_for

        validators: dict[tuple[str, str], Any] = {}
        try:
            for name, tool in self._tools.items():
                input_cls = validator_for(tool.input_schema)
                input_cls.check_schema(tool.input_schema)
                validators[(name, "input")] = input_cls(tool.input_schema)
                if tool.output_schema is not None:
                    output_cls = validator_for(tool.output_schema)
                    output_cls.check_schema(tool.output_schema)
                    validators[(name, "output")] = output_cls(tool.output_schema)
        except Exception as exc:
            logger.exception("invalid registered tool schema")
            raise _ProtocolError(-32603, "Invalid server tool schema") from exc
        self._validators = validators

    def _validator(self, name: str, direction: str) -> Any:
        assert self._validators is not None
        return self._validators[(name, direction)]


class WorkerMcpMount:
    """MCP 2025-11-25 Streamable HTTP endpoint for a ``WorkerMcpServer``."""

    def __init__(
        self,
        server: WorkerMcpServer,
        *,
        path: str = "/mcp",
        trusted_origins: tuple[str, ...] | list[str] = (),
        authorization: Authorization | None = None,
        tool_scopes: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        self.server = server
        self.path = path.rstrip("/") or "/"
        self.trusted_origins = frozenset(trusted_origins)
        self.authorization = authorization
        self.tool_scopes = {
            name: tuple(dict.fromkeys(scopes)) for name, scopes in (tool_scopes or {}).items()
        }

    async def fetch(self, request: Request) -> Response:
        raw = getattr(request, "raw", request)

        if self.authorization is not None and raw.url.pathname == self.authorization.metadata_path:
            if raw.method != "GET":
                return problem(405, title="Method Not Allowed", headers={"allow": "GET"})
            return Response(
                _json_dumps(self.authorization.metadata()),
                headers={"content-type": CONTENT_TYPE_JSON},
            )
        if raw.url.pathname != self.path:
            return problem(404, title="Not Found")
        if not self._origin_allowed(raw):
            return problem(403, title="Origin not allowed")
        if raw.method in ("GET", "DELETE") and not self._protocol_version_ok(raw):
            return problem(400, title="Unsupported MCP-Protocol-Version")

        principal: Principal | None = None
        if self.authorization is not None:
            authorization_header = raw.headers.get("authorization")
            principal = await self.authorization.authenticate_request(raw)
            if principal is None:
                return self._unauthorized(
                    error="invalid_token" if authorization_header is not None else None
                )
            missing = self.authorization.missing_scopes(principal)
            if missing:
                return self._insufficient_scope(self.authorization.required_scopes)

        with principal_context(principal):
            if raw.method == "POST":
                return await self._post(raw, principal)
            if raw.method == "GET":
                return problem(405, title="Method Not Allowed", headers={"allow": "POST"})
            if raw.method == "DELETE":
                return Response(None, status=200)
            return problem(405, title="Method Not Allowed", headers={"allow": "POST, DELETE"})

    async def _post(self, raw: Request, principal: Principal | None) -> Response:
        if not _accepts(raw, CONTENT_TYPE_JSON, CONTENT_TYPE_SSE):
            return problem(
                406,
                title="Not Acceptable",
                detail="MCP POST requests must accept application/json and text/event-stream",
            )
        if _media_type(raw.headers.get("content-type")) != CONTENT_TYPE_JSON:
            return problem(
                415,
                title="Unsupported Media Type",
                detail="MCP POST requests must use application/json",
            )
        try:
            message = json.loads(
                (await raw.bytes()).decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError):
            return problem(400, title="Body must be UTF-8 JSON")

        kind = _message_kind(message)
        if kind is None:
            return problem(400, title="Body must be a single JSON-RPC message")
        is_initialize = kind == "request" and message["method"] == "initialize"
        if not is_initialize and not self._protocol_version_ok(raw):
            return problem(400, title="Unsupported MCP-Protocol-Version")
        if kind != "request":
            # Notifications and client responses do not receive JSON-RPC
            # responses over Streamable HTTP.
            return Response(None, status=202)

        if self.authorization is not None:
            assert principal is not None
            required = self._required_tool_scopes(message)
            missing = self.authorization.missing_scopes(principal, required)
            if missing:
                return self._insufficient_scope(list(required))

        try:
            result = await self.server.dispatch(message)
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except _ProtocolError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not _MISSING:
                error["data"] = exc.data
            response = {"jsonrpc": "2.0", "id": message["id"], "error": error}
            status = exc.status
            headers = exc.headers
        else:
            status = 200
            headers = {}
        return Response(
            _json_dumps(response),
            status=status,
            headers={**headers, "content-type": CONTENT_TYPE_JSON},
        )

    def _protocol_version_ok(self, raw: Request) -> bool:
        return raw.headers.get(PROTOCOL_VERSION_HEADER) == PROTOCOL_VERSION

    def _origin_allowed(self, raw: Request) -> bool:
        return origin_allowed(
            raw.headers.get("origin"),
            raw.url.origin,
            self.trusted_origins,
        )

    def _required_tool_scopes(self, message: dict[str, Any]) -> tuple[str, ...]:
        if message["method"] != "tools/call":
            return ()
        params = message.get("params", {})
        name = params.get("name")
        return self.tool_scopes.get(name, ()) if isinstance(name, str) else ()

    def _unauthorized(self, *, error: str | None = None) -> Response:
        authorization = self.authorization
        assert authorization is not None
        response = problem(401, title="Authorization required")
        response.headers.set(
            "www-authenticate",
            authorization.www_authenticate(error, scopes=tuple(authorization.required_scopes)),
        )
        return response

    def _insufficient_scope(self, required: list[str]) -> Response:
        authorization = self.authorization
        assert authorization is not None
        response = problem(
            403,
            title="Insufficient scope",
            extensions={"required_scopes": required},
        )
        response.headers.set(
            "www-authenticate",
            authorization.www_authenticate("insufficient_scope", scopes=tuple(required)),
        )
        return response

    def register(self, app: Any) -> None:
        async def mcp_handler(context: Context) -> Response:
            with request_context(context):
                return await self.fetch(context.req.raw)

        for method in ("GET", "POST", "DELETE"):
            app.on(method, self.path)(mcp_handler)
        if self.authorization is not None:
            app.on("GET", self.authorization.metadata_path)(mcp_handler)


def _implementation_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and bool(value["name"])
        and isinstance(value.get("version"), str)
        and bool(value["version"])
    )


def _message_kind(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return None
    method = value.get("method")
    if isinstance(method, str):
        if "params" in value and not isinstance(value["params"], dict):
            return None
        if "id" not in value:
            return "notification"
        return "request" if _request_id_valid(value["id"]) else None
    has_result = "result" in value
    has_error = "error" in value
    if has_result == has_error:
        return None
    if has_result:
        if (
            "id" not in value
            or not _request_id_valid(value["id"])
            or not isinstance(value["result"], dict)
        ):
            return None
    else:
        if "id" in value and not _request_id_valid(value["id"]):
            return None
        error = value["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("message"), str)
        ):
            return None
    return "response"


def _request_id_valid(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _normalize_tool_result(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return _text_result(value)
    if isinstance(value, list):
        result: dict[str, Any] = {"content": value}
    elif isinstance(value, dict):
        result = dict(value)
    else:
        raise ToolError("Tool returned an unsupported result")

    content = result.get("content")
    if not isinstance(content, list) or not all(_content_block_valid(item) for item in content):
        raise ToolError("Tool result content must be a list of MCP content blocks")
    if "isError" in result and not isinstance(result["isError"], bool):
        raise ToolError("Tool result isError must be a boolean")
    if "structuredContent" in result and not isinstance(result["structuredContent"], dict):
        raise ToolError("Tool result structuredContent must be an object")
    if "_meta" in result and not isinstance(result["_meta"], dict):
        raise ToolError("Tool result _meta must be an object")
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolError("Tool result must be JSON serializable") from exc
    return result


def _content_block_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not _content_annotations_valid(value):
        return False
    block_type = value.get("type")
    if block_type == "text":
        return isinstance(value.get("text"), str)
    if block_type in ("image", "audio"):
        return isinstance(value.get("data"), str) and isinstance(value.get("mimeType"), str)
    if block_type == "resource_link":
        if not all(isinstance(value.get(key), str) for key in ("name", "uri")):
            return False
        for key in ("title", "description", "mimeType"):
            if key in value and not isinstance(value[key], str):
                return False
        if "size" in value and (
            not isinstance(value["size"], int) or isinstance(value["size"], bool)
        ):
            return False
        return "icons" not in value or (
            isinstance(value["icons"], list) and all(_icon_valid(icon) for icon in value["icons"])
        )
    if block_type == "resource":
        return _resource_contents_valid(value.get("resource"))
    return False


def _content_annotations_valid(value: dict[str, Any]) -> bool:
    if "_meta" in value and not isinstance(value["_meta"], dict):
        return False
    if "annotations" not in value:
        return True
    annotations = value["annotations"]
    if not isinstance(annotations, dict):
        return False
    if "audience" in annotations and (
        not isinstance(annotations["audience"], list)
        or not all(role in ("assistant", "user") for role in annotations["audience"])
    ):
        return False
    if "lastModified" in annotations and not isinstance(annotations["lastModified"], str):
        return False
    if "priority" in annotations:
        priority = annotations["priority"]
        if (
            not isinstance(priority, (int, float))
            or isinstance(priority, bool)
            or not 0 <= priority <= 1
        ):
            return False
    return True


def _resource_contents_valid(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("uri"), str)
        or ("_meta" in value and not isinstance(value["_meta"], dict))
        or ("mimeType" in value and not isinstance(value["mimeType"], str))
    ):
        return False
    return isinstance(value.get("text"), str) or isinstance(value.get("blob"), str)


def _icon_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("src"), str):
        return False
    if "mimeType" in value and not isinstance(value["mimeType"], str):
        return False
    if "theme" in value and value["theme"] not in ("dark", "light"):
        return False
    return "sizes" not in value or (
        isinstance(value["sizes"], list) and all(isinstance(size, str) for size in value["sizes"])
    )


def _tool_annotations_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if "title" in value and not isinstance(value["title"], str):
        return False
    return all(
        key not in value or isinstance(value[key], bool)
        for key in ("destructiveHint", "idempotentHint", "openWorldHint", "readOnlyHint")
    )


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


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


def _json_dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")
