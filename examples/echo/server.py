"""The v0.1 acceptance server: an MCP echo tool mounted on a hayate app.

    uv run --project ../.. uvicorn server:app --port 8930

Then connect any Streamable HTTP client to http://127.0.0.1:8930/mcp —
MCP Inspector, Claude Code (`claude mcp add --transport http`), or the
official SDK client (tests/test_e2e_client.py drives exactly that).
"""

import mcp.types as types
from hayate import Context, Hayate
from mcp.server.lowlevel import Server

from hayate_mcp import McpMount

server = Server("hayate-echo")


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
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"echo: {arguments['text']}")]


app = Hayate()
McpMount(server, path="/mcp").register(app)


@app.get("/")
async def home(c: Context):
    return c.json({"mcp_endpoint": "/mcp", "server": "hayate-echo"})
