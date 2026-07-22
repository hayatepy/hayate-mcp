"""Remote MCP on Cloudflare Python Workers — stateless, no Durable Object.

Each request runs the SDK Server to completion on its own (``stateless=True``),
so there is no long-lived task to keep warm: a plain Worker suffices. This is
the mode that actually runs on Workers, where a bounded request cannot host a
detached ``server.run`` (docs/research/workers-do.md).

Stateful sessions with a server-initiated GET SSE stream stay the ASGI story
(examples/echo); a Durable-Object-backed stateful mode is future work.

    uv run pywrangler dev      # local workerd
    uv run pywrangler deploy   # to Cloudflare
"""

from hayate import Context, Hayate
from hayate.adapters.workers import to_workers


def build_server():
    # mcp is imported here, never at Worker global scope (its jsonschema/rpds
    # chain seeds entropy at import, which workerd forbids in global scope).
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
    # Import + mount are built lazily on first request (entropy-safe scope).
    from hayate_mcp import McpMount

    mount = getattr(app, "_mcp_mount", None)
    if mount is None:
        mount = McpMount(build_server(), path="/mcp", stateless=True)
        app._mcp_mount = mount
    return await mount.fetch(c.req)


Default = to_workers(app)
