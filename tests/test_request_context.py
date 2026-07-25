"""Mounted tool handlers can reuse the active Hayate request context."""

import asyncio

import mcp.types as types
from hayate import Hayate
from mcp.server.lowlevel import Server

from hayate_mcp import (
    LazyMcpMount,
    McpMount,
    WorkerMcpMount,
    WorkerMcpServer,
    get_request_context,
)


async def _call_tool(app: Hayate, marker: str) -> dict:
    response = await app.request(
        "/mcp",
        method="POST",
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
            "x-request-marker": marker,
        },
        json={
            "jsonrpc": "2.0",
            "id": marker,
            "method": "tools/call",
            "params": {"name": "request_marker", "arguments": {}},
        },
    )
    assert response.status == 200
    return await response.json()


async def test_sdk_tool_receives_isolated_request_context() -> None:
    server = Server("context-tools")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="request_marker",
                inputSchema={"type": "object", "additionalProperties": False},
            )
        ]

    @server.call_tool()
    async def call_tool(_name: str, _arguments: dict) -> list[types.TextContent]:
        await asyncio.sleep(0)
        context = get_request_context()
        assert context is not None
        return [
            types.TextContent(
                type="text",
                text=context.req.header("x-request-marker") or "",
            )
        ]

    app = Hayate()
    McpMount(server, stateless=True).register(app)

    first, second = await asyncio.gather(_call_tool(app, "first"), _call_tool(app, "second"))

    assert first["result"]["content"][0]["text"] == "first"
    assert second["result"]["content"][0]["text"] == "second"
    assert get_request_context() is None


async def test_worker_tool_receives_isolated_request_context() -> None:
    server = WorkerMcpServer("context-tools", version="1")

    @server.tool(
        name="request_marker",
        input_schema={"type": "object", "additionalProperties": False},
    )
    async def request_marker(_arguments: dict) -> str:
        await asyncio.sleep(0)
        context = get_request_context()
        assert context is not None
        return context.req.header("x-request-marker") or ""

    app = Hayate()
    WorkerMcpMount(server).register(app)

    first, second = await asyncio.gather(_call_tool(app, "first"), _call_tool(app, "second"))

    assert first["result"]["content"][0]["text"] == "first"
    assert second["result"]["content"][0]["text"] == "second"
    assert get_request_context() is None


async def test_lazy_mount_propagates_request_context() -> None:
    server = WorkerMcpServer("context-tools", version="1")

    @server.tool(
        name="request_marker",
        input_schema={"type": "object", "additionalProperties": False},
    )
    async def request_marker(_arguments: dict) -> str:
        context = get_request_context()
        assert context is not None
        return context.req.header("x-request-marker") or ""

    app = Hayate()
    LazyMcpMount(lambda _context: WorkerMcpMount(server)).register(app)

    result = await _call_tool(app, "lazy")

    assert result["result"]["content"][0]["text"] == "lazy"
    assert get_request_context() is None
