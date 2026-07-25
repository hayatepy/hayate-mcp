"""MCP 2025-11-25 on Cloudflare Python Workers — stateless, no Durable Object.

The Workers-native tools runtime has no Pydantic dependency and no long-lived
task: a plain Worker suffices. It advertises only the MCP capabilities it
implements (tools), while ASGI can continue to use the full official SDK.

Stateful sessions with a server-initiated GET SSE stream stay the ASGI story
(examples/echo); a Durable-Object-backed stateful mode is future work.

    uv run pywrangler dev      # local workerd
    uv run pywrangler deploy   # to Cloudflare
"""

import sys

from hayate import Context, Hayate

from hayate_mcp import WorkerMcpMount, WorkerMcpServer, get_request_context


def build_server() -> WorkerMcpServer:
    server = WorkerMcpServer("hayate-echo-workers", version="0.1.0")

    @server.tool(
        name="echo",
        description="Echo the input back.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        execution={"taskSupport": "forbidden"},
    )
    async def echo(arguments: dict) -> str:
        assert get_request_context() is not None
        return f"echo: {arguments['text']}"

    return server


app = Hayate()


@app.get("/")
async def home(c: Context):
    return c.json({"mcp_endpoint": "/mcp", "runtime": "cloudflare-python-workers"})


WorkerMcpMount(build_server(), path="/mcp").register(app)


if sys.platform == "emscripten":
    from hayate.adapters.workers import to_workers

    Default = to_workers(app)
