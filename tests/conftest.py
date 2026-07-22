import json

import mcp.types as types
import pytest
from hayate import Request
from mcp.server.lowlevel import Server

from hayate_mcp import McpMount

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def call_tool(text: str, request_id: int = 3) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": text}},
    }


def build_server() -> Server:
    server = Server("test-tools")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="Echo the input back.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    @server.call_tool()
    async def handle_call(name: str, arguments: dict) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=f"echo: {arguments['text']}")]

    return server


@pytest.fixture
async def mount():
    m = McpMount(build_server())
    yield m
    await m.store.close_all()


def rpc_request(
    payload: dict | list | str,
    *,
    session_id: str | None = None,
    method: str = "POST",
    origin: str | None = None,
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
) -> Request:
    merged = {"content-type": "application/json", "accept": "application/json", **(headers or {})}
    if session_id is not None:
        merged["mcp-session-id"] = session_id
    if origin is not None:
        merged["origin"] = origin
    headers = merged
    body = payload if isinstance(payload, str) else json.dumps(payload)
    if method in ("GET", "DELETE"):
        body = None
    return Request(f"http://localhost{path}", method=method, headers=headers, body=body)


async def handshake(mount) -> str:
    """initialize + initialized; returns the session id."""
    res = await mount.fetch(rpc_request(INITIALIZE))
    assert res.status == 200
    session_id = res.headers.get("mcp-session-id")
    assert session_id
    accepted = await mount.fetch(rpc_request(INITIALIZED, session_id=session_id))
    assert accepted.status == 202
    return session_id
