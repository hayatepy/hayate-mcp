"""Remote MCP on Cloudflare Python Workers.

The same echo server as examples/echo, but each session lives in its own
Durable Object so it survives isolate recycling (DESIGN §4). The outer app
is a stateless router; the transport runs inside the DO.

    uv run pywrangler dev      # local workerd
    uv run pywrangler deploy   # to Cloudflare
"""

from hayate import Context, Hayate
from hayate.adapters.workers import to_durable_object, to_workers

from hayate_mcp.workers import mcp_durable_object, route_to_session


def build_server():
    # mcp is imported here, never at Worker global scope (its jsonschema
    # dependency does a globally-disallowed op at import — hayate_mcp.workers
    # explains the discipline).
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server("hayate-echo-workers")

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

    return server


app = Hayate()


@app.get("/")
async def home(c: Context):
    return c.json({"mcp_endpoint": "/mcp", "runtime": "cloudflare-python-workers"})


@app.on("GET", "/mcp")
@app.on("POST", "/mcp")
@app.on("DELETE", "/mcp")
async def mcp_route(c: Context):
    return await route_to_session(c, c.env.MCP_SESSION)


# The factory name is the Durable Object class name; it must match
# ``class_name`` in wrangler.toml.
McpSession = to_durable_object(mcp_durable_object(build_server, path="/mcp"))

Default = to_workers(app)
