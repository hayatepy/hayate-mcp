"""Workers-native MCP 2025-11-25 runtime and Streamable HTTP contract."""

from __future__ import annotations

import json

import pytest
from hayate import Request
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.types import JSONRPCMessage

from hayate_mcp import (
    Authorization,
    ToolError,
    WorkerMcpMount,
    WorkerMcpServer,
    WorkerProtocolError,
    get_principal,
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    },
}


def request(
    message,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    origin: str | None = None,
    path: str = "/mcp",
    url: str | None = None,
    protocol_version: str | None = "2025-11-25",
):
    merged = {
        "content-type": "application/json",
        "accept": (
            "text/event-stream" if method == "GET" else "application/json, text/event-stream"
        ),
    }
    if protocol_version is not None:
        merged["mcp-protocol-version"] = protocol_version
    merged.update(headers or {})
    if origin is not None:
        merged["origin"] = origin
    body = message if isinstance(message, str) else json.dumps(message)
    if method in ("GET", "DELETE"):
        body = None
    return Request(
        url or f"http://localhost{path}",
        method=method,
        headers=merged,
        body=body,
    )


def build_server() -> WorkerMcpServer:
    server = WorkerMcpServer(
        "worker-tools",
        version="1.2.3",
        title="Worker Tools",
        instructions="Call echo with text.",
    )

    @server.tool(
        name="echo",
        title="Echo",
        description="Echo text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": True},
        icons=({"src": "https://example.test/echo.png", "mimeType": "image/png"},),
        meta={"com.example/audit": "read-only"},
        execution={"taskSupport": "forbidden"},
    )
    async def echo(arguments):
        return f"echo: {arguments['text']}"

    @server.tool(
        name="structured",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    def structured(_arguments):
        return {
            "content": [{"type": "text", "text": "42"}],
            "structuredContent": {"answer": 42},
        }

    return server


@pytest.fixture
def worker_mount():
    return WorkerMcpMount(build_server())


async def test_initialize_negotiates_latest_and_advertises_only_tools(worker_mount):
    response = await worker_mount.fetch(request(INITIALIZE))
    assert response.status == 200
    body = await response.json()
    assert body["result"] == {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": "worker-tools",
            "version": "1.2.3",
            "title": "Worker Tools",
        },
        "instructions": "Call echo with text.",
    }
    JSONRPCMessage.model_validate(body)

    older = {
        **INITIALIZE,
        "id": "old",
        "params": {**INITIALIZE["params"], "protocolVersion": "2025-06-18"},
    }
    negotiated = await worker_mount.fetch(request(older))
    assert (await negotiated.json())["result"]["protocolVersion"] == "2025-11-25"


async def test_tools_list_and_call_are_accepted_by_official_sdk_models(worker_mount):
    listed = await worker_mount.fetch(request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    body = await listed.json()
    JSONRPCMessage.model_validate(body)
    tool = body["result"]["tools"][0]
    assert tool["name"] == "echo"
    assert tool["title"] == "Echo"
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["_meta"] == {"com.example/audit": "read-only"}
    assert tool["execution"] == {"taskSupport": "forbidden"}

    called = await worker_mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "edge"}},
            }
        )
    )
    call_body = await called.json()
    JSONRPCMessage.model_validate(call_body)
    assert call_body["result"] == {"content": [{"type": "text", "text": "echo: edge"}]}


async def test_structured_output_is_checked_against_output_schema(worker_mount):
    response = await worker_mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "structured", "arguments": {}},
            }
        )
    )
    assert (await response.json())["result"]["structuredContent"] == {"answer": 42}


async def test_invalid_arguments_are_a_model_correctable_tool_error(worker_mount):
    response = await worker_mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "", "extra": True}},
            }
        )
    )
    result = (await response.json())["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("Input validation error:")
    assert "non-empty" in result["content"][0]["text"]
    assert "Additional properties" in result["content"][0]["text"]


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "missing"}, -32601),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            },
            -32602,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "x"}, "task": {}},
            },
            -32601,
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": "unknown"}},
            -32602,
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": None}},
            -32602,
        ),
    ],
)
async def test_protocol_errors_have_standard_codes(worker_mount, message, code):
    response = await worker_mount.fetch(request(message))
    assert (await response.json())["error"]["code"] == code


async def test_ping_and_notifications(worker_mount):
    ping = await worker_mount.fetch(request({"jsonrpc": "2.0", "id": "p", "method": "ping"}))
    assert (await ping.json()) == {"jsonrpc": "2.0", "id": "p", "result": {}}

    initialized = await worker_mount.fetch(
        request({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert initialized.status == 202
    assert await initialized.bytes() == b""

    client_response = await worker_mount.fetch(request({"jsonrpc": "2.0", "id": 7, "result": {}}))
    assert client_response.status == 202


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "result": []},
        {"jsonrpc": "2.0", "id": 1, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "bad"}},
    ],
)
async def test_invalid_json_rpc_envelopes_are_rejected(worker_mount, payload):
    response = await worker_mount.fetch(request(payload))
    assert response.status == 400


_JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=8) | st.dictionaries(st.text(max_size=32), children, max_size=8)
    ),
    max_leaves=24,
)


@given(_JSON_VALUES)
@settings(max_examples=200, deadline=None)
async def test_arbitrary_json_never_escapes_the_transport_or_emits_invalid_json_rpc(payload):
    response = await WorkerMcpMount(build_server()).fetch(request(payload))
    assert response.status in (200, 202, 400)
    if response.status == 200:
        JSONRPCMessage.model_validate(await response.json())
    elif response.status == 202:
        assert await response.bytes() == b""


async def test_transport_media_origin_version_and_methods(worker_mount):
    wrong_type = await worker_mount.fetch(
        request(INITIALIZE, headers={"content-type": "text/plain"})
    )
    assert wrong_type.status == 415
    wrong_accept = await worker_mount.fetch(
        request(INITIALIZE, headers={"accept": "application/json"})
    )
    assert wrong_accept.status == 406
    wrong_origin = await worker_mount.fetch(request(INITIALIZE, origin="https://evil.example"))
    assert wrong_origin.status == 403
    null_origin = await worker_mount.fetch(request(INITIALIZE, origin="null"))
    assert null_origin.status == 403
    reflected_host = request(
        INITIALIZE,
        origin="https://evil.example",
        url="https://evil.example/mcp",
    )
    assert (await worker_mount.fetch(reflected_host)).status == 403

    bad_version = await worker_mount.fetch(
        request(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"mcp-protocol-version": "2099-01-01"},
        )
    )
    assert bad_version.status == 400
    initialize_exempt = await worker_mount.fetch(
        request(INITIALIZE, headers={"mcp-protocol-version": "2099-01-01"})
    )
    assert initialize_exempt.status == 200
    initialize_without_header = await worker_mount.fetch(request(INITIALIZE, protocol_version=None))
    assert initialize_without_header.status == 200
    missing_version = await worker_mount.fetch(
        request(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            protocol_version=None,
        )
    )
    assert missing_version.status == 400
    notification_bad_version = await worker_mount.fetch(
        request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            protocol_version="2099-01-01",
        )
    )
    assert notification_bad_version.status == 400
    response_bad_version = await worker_mount.fetch(
        request(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            protocol_version="2099-01-01",
        )
    )
    assert response_bad_version.status == 400

    get = await worker_mount.fetch(request("", method="GET"))
    assert get.status == 405
    assert get.headers.get("allow") == "POST"
    delete = await worker_mount.fetch(request("", method="DELETE"))
    assert delete.status == 200
    other = await worker_mount.fetch(request("", method="PUT"))
    assert other.status == 405


async def test_expected_tool_errors_are_visible_and_exceptions_are_sanitized(caplog):
    server = WorkerMcpServer("errors", version="1")

    @server.tool(name="expected", input_schema={"type": "object"})
    def expected(_arguments):
        raise ToolError("try another value")

    @server.tool(name="unexpected", input_schema={"type": "object"})
    def unexpected(_arguments):
        raise RuntimeError("secret internals")

    mount = WorkerMcpMount(server)
    expected_response = await mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "expected", "arguments": {}},
            }
        )
    )
    assert (await expected_response.json())["result"] == {
        "content": [{"type": "text", "text": "try another value"}],
        "isError": True,
    }

    unexpected_response = await mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "unexpected", "arguments": {}},
            }
        )
    )
    result = (await unexpected_response.json())["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Tool execution failed"
    assert "secret internals" not in json.dumps(result)
    assert "secret internals" in caplog.text


async def test_tool_handler_can_preserve_http_error_semantics() -> None:
    server = WorkerMcpServer("protected", version="1")

    @server.tool(name="protected", input_schema={"type": "object"})
    def protected(_arguments):
        raise WorkerProtocolError(
            -32001,
            "Authentication is required.",
            status=401,
            headers={
                "content-type": "text/plain",
                "www-authenticate": "Bearer",
            },
        )

    response = await WorkerMcpMount(server).fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "protected", "arguments": {}},
            }
        )
    )

    assert response.status == 401
    assert response.headers.get("content-type") == "application/json"
    assert response.headers.get("www-authenticate") == "Bearer"
    assert (await response.json())["error"] == {
        "code": -32001,
        "message": "Authentication is required.",
    }


@pytest.mark.parametrize(
    "execution",
    [
        {"taskSupport": "optional"},
        {"taskSupport": "required"},
        {"taskSupport": "forbidden", "unknown": True},
    ],
)
def test_worker_tool_rejects_unadvertised_task_execution(execution) -> None:
    server = WorkerMcpServer("tasks", version="1")
    with pytest.raises(TypeError, match="taskSupport='forbidden'"):

        @server.tool(name="task", input_schema={"type": "object"}, execution=execution)
        def task(_arguments):
            return "no"


async def test_all_2025_11_25_content_block_shapes_are_emitted(worker_mount):
    server = WorkerMcpServer("content", version="1")

    @server.tool(name="blocks", input_schema={"type": "object"})
    def blocks(_arguments):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "annotations": {
                        "audience": ["assistant", "user"],
                        "priority": 0.5,
                        "lastModified": "2025-01-12T15:00:58Z",
                    },
                },
                {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
                {"type": "audio", "data": "YXVkaW8=", "mimeType": "audio/wav"},
                {
                    "type": "resource_link",
                    "name": "guide",
                    "uri": "https://example.test/guide",
                    "title": "Guide",
                    "description": "A guide.",
                    "mimeType": "text/plain",
                    "size": 42,
                    "icons": [
                        {
                            "src": "https://example.test/icon.svg",
                            "mimeType": "image/svg+xml",
                            "sizes": ["any"],
                            "theme": "dark",
                        }
                    ],
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/readme",
                        "mimeType": "text/plain",
                        "text": "read me",
                    },
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/data",
                        "mimeType": "application/octet-stream",
                        "blob": "ZGF0YQ==",
                    },
                },
            ]
        }

    response = await WorkerMcpMount(server).fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "blocks", "arguments": {}},
            }
        )
    )
    body = await response.json()
    JSONRPCMessage.model_validate(body)
    assert [item["type"] for item in body["result"]["content"]] == [
        "text",
        "image",
        "audio",
        "resource_link",
        "resource",
        "resource",
    ]


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text"}],
        [{"type": "image", "data": "aW1hZ2U="}],
        [{"type": "audio", "data": "YXVkaW8=", "mimeType": 1}],
        [{"type": "resource_link", "name": "x", "uri": "https://example.test", "size": True}],
        [{"type": "resource", "resource": {"uri": "https://example.test"}}],
        [{"type": "future"}],
        [{"type": "text", "text": "x", "annotations": {"priority": 2}}],
    ],
)
async def test_invalid_content_blocks_become_model_visible_tool_errors(content):
    server = WorkerMcpServer("content-errors", version="1")

    @server.tool(name="invalid", input_schema={"type": "object"})
    def invalid(_arguments):
        return {"content": content}

    response = await WorkerMcpMount(server).fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "invalid", "arguments": {}},
            }
        )
    )
    result = (await response.json())["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Tool result content must be a list of MCP content blocks"
    )


async def test_worker_authorization_scopes_and_principal():
    async def verify(token):
        if token == "good":
            return {"sub": "user-1", "scope": "mcp echo:call"}
        return None

    server = WorkerMcpServer("secured", version="1")

    @server.tool(name="whoami", input_schema={"type": "object"})
    def whoami(_arguments):
        principal = get_principal()
        assert principal is not None
        return principal["subject"]

    mount = WorkerMcpMount(
        server,
        authorization=Authorization(
            resource="http://localhost/mcp",
            authorization_servers=["http://localhost"],
            verify_token=verify,
            scopes_supported=["mcp", "echo:call"],
            required_scopes=["mcp"],
        ),
        tool_scopes={"whoami": ["echo:call"]},
    )
    unauthenticated = await mount.fetch(
        request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    assert unauthenticated.status == 401

    called = await mount.fetch(
        request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
            headers={"authorization": "Bearer good"},
        )
    )
    assert (await called.json())["result"]["content"][0]["text"] == "user-1"


async def test_worker_request_aware_dpop_authorization():
    async def verify_request(raw):
        if (
            raw.method == "POST"
            and raw.headers.get("authorization") == "DPoP worker-token"
            and raw.headers.get("dpop") == "worker-proof"
        ):
            return {"sub": "worker-dpop-user", "scope": "mcp"}
        return None

    server = WorkerMcpServer("secured-dpop", version="1")
    mount = WorkerMcpMount(
        server,
        authorization=Authorization(
            resource="http://localhost/mcp",
            authorization_servers=["http://localhost"],
            verify_request=verify_request,
            authorization_scheme="DPoP",
            required_scopes=["mcp"],
        ),
    )
    denied = await mount.fetch(request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert denied.status == 401
    assert denied.headers.get("www-authenticate").startswith("DPoP ")

    accepted = await mount.fetch(
        request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={
                "authorization": "DPoP worker-token",
                "dpop": "worker-proof",
            },
        )
    )
    assert accepted.status == 200


def test_tool_registration_rejects_invalid_definitions():
    server = WorkerMcpServer("validation", version="1")
    with pytest.raises(ValueError, match="tool name"):
        server.tool(name="contains a space", input_schema={"type": "object"})(lambda _: "")
    with pytest.raises(ValueError, match="input_schema"):
        server.tool(name="bad", input_schema={"type": "array"})(lambda _: "")

    server.tool(name="once", input_schema={"type": "object"})(lambda _: "")
    with pytest.raises(ValueError, match="already registered"):
        server.tool(name="once", input_schema={"type": "object"})(lambda _: "")
