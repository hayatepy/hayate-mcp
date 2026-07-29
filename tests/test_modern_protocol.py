"""MCP 2026-07-28 dual-era transport and tools contract."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
import pytest
from hayate import Request
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ToolsListChanged,
)

from conftest import build_server
from hayate_mcp import (
    Authorization,
    McpMount,
    WorkerMcpMount,
    WorkerMcpServer,
    WorkerProtocolError,
)
from hayate_mcp.protocol import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    SERVER_INFO_META_KEY,
    encode_header_value,
    validate_mcp_param_headers,
)


def modern_message(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: str | int = 1,
    version: str = MODERN_PROTOCOL_VERSION,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            "_meta": {
                PROTOCOL_VERSION_META_KEY: version,
                CLIENT_CAPABILITIES_META_KEY: {},
                CLIENT_INFO_META_KEY: {
                    "name": "hayate-modern-tests",
                    "version": "1.0.0",
                },
            },
            **(params or {}),
        },
    }


def modern_request(
    message: dict[str, Any],
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
) -> Request:
    routing = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": message["params"]["_meta"][PROTOCOL_VERSION_META_KEY],
        "mcp-method": message["method"],
    }
    name_key = {
        "tools/call": "name",
        "prompts/get": "name",
        "resources/read": "uri",
    }.get(message["method"])
    if name_key is not None and name_key in message["params"]:
        routing["mcp-name"] = encode_header_value(str(message["params"][name_key]))
    routing.update(headers or {})
    return Request(
        "http://localhost/mcp",
        method=method,
        headers=routing,
        body=None if method != "POST" else json.dumps(message),
    )


def modern_request_with_duplicate(
    message: dict[str, Any],
    name: str,
    value: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    base = modern_request(message, headers=headers)
    return Request(
        "http://localhost/mcp",
        method="POST",
        headers=[*base.headers.raw(), (name, value)],
        body=json.dumps(message),
    )


def modern_worker() -> WorkerMcpServer:
    server = WorkerMcpServer(
        "modern-worker",
        version="0.12.0",
        title="Modern Worker",
        description="A stateless edge-native MCP server.",
        website_url="https://hayatepy.dev/",
        instructions="Prefer echo for text round trips.",
        tools_ttl_ms=60_000,
        cache_scope="public",
    )

    @server.tool(
        name="echo",
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "x-mcp-header": "Text",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    def echo(arguments: dict[str, Any]) -> str:
        return f"echo: {arguments['text']}"

    @server.tool(
        name="numbers",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "array",
            "items": {"type": "integer"},
        },
    )
    def numbers(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "1, 2, 3"}],
            "structuredContent": [1, 2, 3],
        }

    @server.tool(
        name="guarded",
        input_schema={"type": "object", "properties": {}},
        required_capabilities={"sampling": {}},
    )
    def guarded(_arguments: dict[str, Any]) -> str:
        return "sampling is available"

    return server


@pytest.fixture
def modern_worker_mount() -> WorkerMcpMount:
    return WorkerMcpMount(modern_worker())


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_discovery_is_stateless_and_self_describing(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    response = await mount.fetch(modern_request(modern_message("server/discover")))

    assert response.status == 200
    assert response.headers.get("mcp-session-id") is None
    result = (await response.json())["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"]["tools"] is not None
    assert result["ttlMs"] >= 0
    assert result["cacheScope"] in ("public", "private")
    assert result["_meta"][SERVER_INFO_META_KEY]["name"]

    if isinstance(mount, McpMount):
        assert mount.store._sessions == {}


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_malformed_json_returns_jsonrpc_parse_error(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    response = await mount.fetch(
        Request(
            "http://localhost/mcp",
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": MODERN_PROTOCOL_VERSION,
                "mcp-method": "server/discover",
            },
            body="{",
        )
    )
    body = await response.json()

    assert response.status == 400
    assert body["id"] is None
    assert body["error"]["code"] == -32700


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_invalid_jsonrpc_envelope_returns_jsonrpc_error(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    message = modern_message("server/discover")
    message["id"] = None
    response = await mount.fetch(modern_request(message))
    body = await response.json()

    assert response.status == 400
    assert body["id"] is None
    assert body["error"]["code"] == -32600


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_tools_results_have_required_envelopes(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    listed = await mount.fetch(modern_request(modern_message("tools/list")))
    list_result = (await listed.json())["result"]

    assert list_result["resultType"] == "complete"
    assert list_result["ttlMs"] >= 0
    assert list_result["cacheScope"] in ("public", "private")
    assert list_result["_meta"][SERVER_INFO_META_KEY]["name"]
    assert [tool["name"] for tool in list_result["tools"]] == sorted(
        tool["name"] for tool in list_result["tools"]
    )

    call = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": "modern"}},
        request_id=2,
    )
    called = await mount.fetch(
        modern_request(
            call,
            headers={"mcp-param-text": "modern"} if runtime == "workers" else {},
        )
    )
    call_result = (await called.json())["result"]
    assert call_result["resultType"] == "complete"
    assert call_result["content"][0]["text"] == "echo: modern"
    assert call_result["_meta"][SERVER_INFO_META_KEY]["name"]


async def test_asgi_modern_response_stream_delivers_progress_before_result():
    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="progress",
                    input_schema={"type": "object", "properties": {}},
                )
            ]
        )

    async def call_tool(
        ctx: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        meta = params.meta or {}
        token = meta.get("progress_token", meta.get("progressToken"))
        await ctx.session.send_progress_notification(token, 50, total=100)
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])

    server = Server(
        "progress-server",
        version="0.12.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    message = modern_message(
        "tools/call",
        {
            "name": "progress",
            "arguments": {},
            "_meta": {
                PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                CLIENT_CAPABILITIES_META_KEY: {},
                CLIENT_INFO_META_KEY: {
                    "name": "hayate-modern-tests",
                    "version": "1.0.0",
                },
                "progressToken": "progress-1",
            },
        },
    )
    response = await McpMount(server).fetch(modern_request(message))

    assert response.status == 200
    assert response.headers.get("content-type") == "text/event-stream"
    events = [
        json.loads(line.removeprefix("data: "))
        for line in (await response.text()).splitlines()
        if line.startswith("data: ")
    ]
    assert [event.get("method") for event in events[:-1]] == ["notifications/progress"]
    assert events[0]["params"]["progressToken"] == "progress-1"
    assert events[-1]["result"]["content"][0]["text"] == "done"


async def test_asgi_modern_subscription_is_a_live_response_owned_stream():
    bus = InMemorySubscriptionBus()
    listen = ListenHandler(bus)

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[])

    server = Server(
        "subscription-server",
        version="0.12.0",
        on_list_tools=list_tools,
        on_subscriptions_listen=listen,
    )
    message = modern_message(
        "subscriptions/listen",
        {"notifications": {"toolsListChanged": True}},
        request_id="subscription-1",
    )
    response = await McpMount(server).fetch(modern_request(message))

    assert response.status == 200
    assert response.headers.get("content-type") == "text/event-stream"
    assert response.headers.get("x-accel-buffering") == "no"
    stream = response.body
    assert stream is not None and not isinstance(stream, bytes)
    iterator = stream.__aiter__()

    acknowledged = _sse_json(await anext(iterator))
    assert acknowledged["method"] == "notifications/subscriptions/acknowledged"
    assert (
        acknowledged["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"]
        == "subscription-1"
    )

    await bus.publish(ToolsListChanged())
    changed = _sse_json(await anext(iterator))
    assert changed["method"] == "notifications/tools/list_changed"
    assert changed["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == "subscription-1"

    listen.close()
    final = _sse_json(await anext(iterator))
    assert final["id"] == "subscription-1"
    assert final["result"]["resultType"] == "complete"
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


async def test_closing_subscription_response_cancels_its_handler():
    bus = InMemorySubscriptionBus()
    listen = ListenHandler(bus)
    server = Server(
        "subscription-cancellation-server",
        on_subscriptions_listen=listen,
    )
    message = modern_message(
        "subscriptions/listen",
        {"notifications": {"toolsListChanged": True}},
        request_id="subscription-cancel",
    )
    response = await McpMount(server).fetch(modern_request(message))
    stream = response.body
    assert stream is not None and not isinstance(stream, bytes)
    iterator = stream.__aiter__()

    assert _sse_json(await anext(iterator))["method"] == (
        "notifications/subscriptions/acknowledged"
    )
    await iterator.aclose()

    assert not listen._streams


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_removed_initialize_is_method_not_found(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    response = await mount.fetch(modern_request(modern_message("initialize")))

    assert response.status == 404
    assert (await response.json())["error"]["code"] == -32601


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_tool_capabilities_are_enforced(runtime: str):
    if runtime == "asgi":
        server = build_server()
        mount: McpMount | WorkerMcpMount = McpMount(
            server,
            tool_capabilities={"echo": {"sampling": {}}},
        )
        name = "echo"
        arguments = {"text": "guarded"}
    else:
        mount = WorkerMcpMount(modern_worker())
        name = "guarded"
        arguments = {}

    message = modern_message(
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    rejected = await mount.fetch(modern_request(message))

    assert rejected.status == 400
    error = (await rejected.json())["error"]
    assert error["code"] == -32021
    assert error["data"]["requiredCapabilities"] == {"sampling": {}}

    message["params"]["_meta"][CLIENT_CAPABILITIES_META_KEY] = {"sampling": {}}
    accepted = await mount.fetch(modern_request(message))
    assert accepted.status == 200


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_authorization_is_required_on_every_stateless_post(runtime: str):
    async def verify(token: str) -> dict[str, Any] | None:
        if token == "good":
            return {"sub": "modern-user", "scope": "mcp"}
        return None

    authorization = Authorization(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://auth.example.com"],
        verify_token=verify,
        scopes_supported=["mcp"],
        required_scopes=["mcp"],
    )
    mount: McpMount | WorkerMcpMount
    if runtime == "asgi":
        mount = McpMount(build_server(), authorization=authorization)
    else:
        mount = WorkerMcpMount(modern_worker(), authorization=authorization)
    message = modern_message("server/discover")

    denied = await mount.fetch(modern_request(message))
    accepted = await mount.fetch(modern_request(message, headers={"authorization": "Bearer good"}))

    assert denied.status == 401
    assert "resource_metadata=" in denied.headers.get("www-authenticate")
    assert accepted.status == 200


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_modern_tool_scope_precedes_parameter_routing(runtime: str):
    async def verify(_token: str) -> dict[str, Any]:
        return {"sub": "modern-user", "scope": "mcp"}

    authorization = Authorization(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://auth.example.com"],
        verify_token=verify,
        scopes_supported=["mcp", "documents:read"],
    )
    if runtime == "asgi":
        mount: McpMount | WorkerMcpMount = McpMount(
            build_server(),
            authorization=authorization,
            tool_scopes={"echo": ["documents:read"]},
        )
    else:
        mount = WorkerMcpMount(
            modern_worker(),
            authorization=authorization,
            tool_scopes={"echo": ["documents:read"]},
        )
    message = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": "secret"}},
    )
    response = await mount.fetch(
        modern_request(
            message,
            headers={"authorization": "Bearer valid-but-under-scoped"},
        )
    )

    assert response.status == 403
    assert 'scope="documents:read"' in response.headers.get("www-authenticate")


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        (CLIENT_CAPABILITIES_META_KEY, []),
        (CLIENT_INFO_META_KEY, {"name": "missing-version"}),
    ],
)
async def test_modern_metadata_values_are_structurally_validated(
    runtime: str,
    key: str,
    value: Any,
):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    message = modern_message("server/discover")
    message["params"]["_meta"][key] = value
    response = await mount.fetch(modern_request(message))

    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32602


async def test_workers_support_arbitrary_structured_json_only_on_modern_wire(
    modern_worker_mount: WorkerMcpMount,
):
    message = modern_message(
        "tools/call",
        {"name": "numbers", "arguments": {}},
    )
    response = await modern_worker_mount.fetch(modern_request(message))
    assert (await response.json())["result"]["structuredContent"] == [1, 2, 3]

    listed = await modern_worker_mount.fetch(modern_request(modern_message("tools/list")))
    numbers = next(
        tool for tool in (await listed.json())["result"]["tools"] if tool["name"] == "numbers"
    )
    assert numbers["outputSchema"]["type"] == "array"


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"mcp-protocol-version": ""}, -32020),
        ({"mcp-method": ""}, -32020),
        ({"mcp-name": "other"}, -32020),
        ({"mcp-param-text": "other"}, -32020),
    ],
)
async def test_modern_routing_headers_are_cross_checked(
    modern_worker_mount: WorkerMcpMount,
    headers: dict[str, str],
    code: int,
):
    message = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": "header-value"}},
    )
    request_headers = {"mcp-param-text": "header-value", **headers}
    response = await modern_worker_mount.fetch(modern_request(message, headers=request_headers))

    assert response.status == 400
    assert (await response.json())["error"]["code"] == code


async def test_x_mcp_header_supports_unicode_base64_sentinel(
    modern_worker_mount: WorkerMcpMount,
):
    message = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": "疾風"}},
    )
    response = await modern_worker_mount.fetch(
        modern_request(
            message,
            headers={"mcp-param-text": encode_header_value("疾風")},
        )
    )
    assert response.status == 200
    assert (await response.json())["result"]["content"][0]["text"] == "echo: 疾風"


@pytest.mark.parametrize("text", ["café", "line\tbreak"])
async def test_x_mcp_header_rejects_unsafe_unencoded_values(
    modern_worker_mount: WorkerMcpMount,
    text: str,
):
    message = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": text}},
    )
    response = await modern_worker_mount.fetch(
        modern_request(
            message,
            headers={"mcp-param-text": text},
        )
    )

    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32020


def test_x_mcp_header_rejects_integer_outside_javascript_safe_range():
    value = 2**53
    rejection = validate_mcp_param_headers(
        {
            "type": "object",
            "properties": {
                "sequence": {
                    "type": "integer",
                    "x-mcp-header": "Sequence",
                }
            },
        },
        {"sequence": value},
        {"mcp-param-sequence": str(value)},
    )

    assert rejection is not None
    assert rejection.code == -32020


async def test_asgi_cross_checks_x_mcp_header_against_visible_tool_schema():
    async def list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="routed",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "tenant": {
                                "type": "string",
                                "x-mcp-header": "Tenant",
                            }
                        },
                        "required": ["tenant"],
                    },
                )
            ]
        )

    async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=str((params.arguments or {})["tenant"]),
                )
            ]
        )

    mount = McpMount(
        Server(
            "routed-asgi",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
    )
    message = modern_message(
        "tools/call",
        {"name": "routed", "arguments": {"tenant": "acme"}},
    )
    rejected = await mount.fetch(modern_request(message, headers={"mcp-param-tenant": "other"}))
    assert rejected.status == 400
    assert (await rejected.json())["error"]["code"] == -32020

    accepted = await mount.fetch(modern_request(message, headers={"mcp-param-tenant": "acme"}))
    assert accepted.status == 200
    assert (await accepted.json())["result"]["content"][0]["text"] == "acme"


async def test_asgi_cross_checks_x_mcp_header_on_later_tool_page():
    async def list_tools(_ctx, params) -> types.ListToolsResult:
        if params.cursor is None:
            return types.ListToolsResult(tools=[], next_cursor="second")
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="routed",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "tenant": {
                                "type": "string",
                                "x-mcp-header": "Tenant",
                            }
                        },
                        "required": ["tenant"],
                    },
                )
            ]
        )

    async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=str((params.arguments or {})["tenant"]),
                )
            ]
        )

    mount = McpMount(
        Server(
            "paginated-routed-asgi",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
    )
    message = modern_message(
        "tools/call",
        {"name": "routed", "arguments": {"tenant": "acme"}},
    )
    rejected = await mount.fetch(modern_request(message, headers={"mcp-param-tenant": "other"}))

    assert rejected.status == 400
    assert (await rejected.json())["error"]["code"] == -32020


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_duplicate_standard_routing_header_is_rejected(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    message = modern_message("server/discover")
    response = await mount.fetch(
        modern_request_with_duplicate(
            message,
            "Mcp-Method",
            "server/discover",
        )
    )
    error = (await response.json())["error"]

    assert response.status == 400
    assert error["code"] == -32020
    assert "appears more than once" in error["message"]


async def test_duplicate_custom_routing_header_is_rejected(
    modern_worker_mount: WorkerMcpMount,
):
    message = modern_message(
        "tools/call",
        {"name": "echo", "arguments": {"text": "duplicate"}},
    )
    response = await modern_worker_mount.fetch(
        modern_request_with_duplicate(
            message,
            "Mcp-Param-Text",
            "duplicate",
            headers={"mcp-param-text": "duplicate"},
        )
    )
    error = (await response.json())["error"]

    assert response.status == 400
    assert error["code"] == -32020
    assert "appears more than once" in error["message"]


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_unknown_method_is_http_404(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    response = await mount.fetch(modern_request(modern_message("com.example/missing")))
    body = await response.json()

    assert response.status == 404
    assert body["error"]["code"] == -32601


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_unsupported_modern_version_has_negotiation_data(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    message = modern_message("server/discover", version="2099-01-01")
    response = await mount.fetch(modern_request(message))
    error = (await response.json())["error"]

    assert response.status == 400
    assert error["code"] == -32022
    assert error["data"] == {
        "supported": ["2026-07-28"],
        "requested": "2099-01-01",
    }


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
async def test_version_negotiation_precedes_version_specific_metadata_shapes(runtime: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    message = modern_message("server/discover", version="2099-01-01")
    message["params"]["_meta"][CLIENT_CAPABILITIES_META_KEY] = []
    response = await mount.fetch(modern_request(message))

    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32022


@pytest.mark.parametrize("runtime", ["asgi", "workers"])
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_modern_http_only_allows_post(runtime: str, method: str):
    mount = McpMount(build_server()) if runtime == "asgi" else WorkerMcpMount(modern_worker())
    response = await mount.fetch(modern_request(modern_message("server/discover"), method=method))
    assert response.status == 405
    assert response.headers.get("allow") == "POST"


def test_invalid_x_mcp_header_is_rejected_at_registration():
    server = WorkerMcpServer("invalid-header", version="1")
    with pytest.raises(ValueError, match="x-mcp-header"):

        @server.tool(
            name="broken",
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "array",
                        "x-mcp-header": "Value",
                    }
                },
            },
        )
        def broken(_arguments: dict[str, Any]) -> str:
            return "unreachable"


def test_worker_extensions_must_be_json_serializable():
    with pytest.raises(TypeError, match="extensions must be JSON serializable"):
        WorkerMcpServer(
            "invalid-extensions",
            version="1",
            extensions={"com.example/future": {"value": object()}},
        )


async def test_worker_cannot_override_modern_protocol_error_http_mapping():
    server = WorkerMcpServer("mapped-errors", version="1")

    @server.tool(
        name="invalid",
        input_schema={"type": "object", "properties": {}},
    )
    def invalid(_arguments: dict[str, Any]) -> str:
        raise WorkerProtocolError(
            -32602,
            "Invalid params",
            status=503,
        )

    message = modern_message(
        "tools/call",
        {"name": "invalid", "arguments": {}},
    )
    response = await WorkerMcpMount(server).fetch(modern_request(message))

    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32602


def _sse_json(event: bytes) -> dict[str, Any]:
    data = next(
        line.removeprefix(b"data: ") for line in event.splitlines() if line.startswith(b"data: ")
    )
    return json.loads(data)


def test_sdk_server_fixture_uses_complete_v2_result_models():
    async def list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[])

    server = Server("v2-models", on_list_tools=list_tools)
    assert server.name == "v2-models"
