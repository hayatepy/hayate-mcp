"""MCP SDK spike: can the official python-sdk run on workerd? (DESIGN §10)

Disposable code — findings land in docs/research/pyodide.md.

    uv sync && uv run pywrangler dev    # local workerd on :8787
    (Windows: see hayate-auth docs/research/kdf.md for the vendor workaround)

Routes:
    /probe      import every SDK layer, report ok/error per module
    /echo-tool  full in-process protocol round trip:
                initialize -> tools/list -> tools/call over memory streams
"""

import importlib
import sys
import time

from hayate import Context, Hayate
from hayate.adapters.workers import to_workers

app = Hayate()

PROBE_MODULES = [
    "anyio",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "httpx",
    "httpx_sse",
    "jsonschema",
    "starlette",
    "sse_starlette",
    "uvicorn",
    "mcp",
    "mcp.types",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.client.session",
    "mcp.shared.memory",
]


@app.get("/probe")
async def probe(c: Context):
    results: dict[str, str] = {}
    for mod in PROBE_MODULES:
        t0 = time.perf_counter()
        try:
            importlib.import_module(mod)
            results[mod] = f"ok ({round((time.perf_counter() - t0) * 1000)}ms)"
        except Exception as e:
            results[mod] = f"{type(e).__name__}: {e}"
    return c.json({"python": sys.version, "imports": results})


def _build_server():
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server("spike")

    @server.list_tools()
    async def list_tools() -> list:
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
    async def call_tool(name: str, arguments: dict) -> list:
        return [types.TextContent(type="text", text=f"echo: {arguments['text']}")]

    return server


@app.get("/echo-tool")
async def echo_tool(c: Context):
    """Drive the lowlevel Server through a real ClientSession in-process.

    Uses the SDK's own memory-stream test harness, so what's proven here is
    exactly the seam hayate-mcp's transport will attach to."""
    steps: dict[str, str] = {}
    try:
        from mcp.shared.memory import create_connected_server_and_client_session

        server = _build_server()
        steps["build"] = "ok"
        t0 = time.perf_counter()
        async with create_connected_server_and_client_session(server) as session:
            steps["initialize"] = "ok"
            tools = await session.list_tools()
            steps["tools/list"] = ",".join(t.name for t in tools.tools)
            result = await session.call_tool("echo", {"text": "hayate"})
            steps["tools/call"] = result.content[0].text
        steps["total_ms"] = str(round((time.perf_counter() - t0) * 1000))
        return c.json({"ok": True, "steps": steps})
    except Exception as e:
        import traceback

        return c.json(
            {"ok": False, "steps": steps, "error": f"{type(e).__name__}: {e}",
             "trace": traceback.format_exc()[-2000:]},
            status=500,
        )


try:
    Default = to_workers(app)
except ModuleNotFoundError:  # imported on plain CPython
    Default = None
