"""An MCP echo tool mounted on a Hayate app.

    uv run --project ../.. uvicorn server:app --port 8930

Then connect any Streamable HTTP client to http://127.0.0.1:8930/mcp —
MCP Inspector, Claude Code (`claude mcp add --transport http`), or the
official SDK client (tests/test_e2e_client.py drives exactly that).
"""

import mcp.types as types
from hayate import Context, Hayate
from mcp.server.lowlevel import Server

from hayate_mcp import McpMount


async def list_tools(_ctx, _params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="Echo the input back.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    arguments = params.arguments or {}
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"echo: {arguments['text']}",
            )
        ]
    )


server = Server(
    "hayate-echo",
    version="0.12.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


app = Hayate()
McpMount(server, path="/mcp").register(app)


@app.get("/")
async def home(c: Context):
    return c.json({"mcp_endpoint": "/mcp", "server": "hayate-echo"})
