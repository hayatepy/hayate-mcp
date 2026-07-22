# hayate-mcp

Mount an MCP server into a [hayate](https://github.com/hayatepy/hayate) app —
a Streamable HTTP transport that bridges the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) to
WHATWG Request/Response. The [@hono/mcp](https://www.npmjs.com/package/@hono/mcp)
architecture, in Python.

> **Status: alpha (0.2.x).** The transport serves MCP Inspector, Claude Code,
> and the official SDK client (single-JSON POST responses plus the optional
> server-initiated GET SSE stream). The `hayate_mcp.workers` Durable Object
> integration is **experimental** — implemented and unit-tested on CPython,
> but not yet green on workerd (see [docs/research/workers-do.md](docs/research/workers-do.md)).
> The internal design memo (Japanese) lives in [DESIGN.md](DESIGN.md).

```python
from mcp.server.lowlevel import Server   # official SDK — define your tools here
from hayate import Hayate
from hayate_mcp import McpMount

server = Server("my-tools")
# … @server.list_tools() / @server.call_tool() …

app = Hayate()
McpMount(server, path="/mcp").register(app)   # that's the whole integration
```

Serve it with any ASGI server (`uvicorn server:app`), then connect:

```sh
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp --transport http --method tools/list
```

```sh
claude mcp add my-tools --transport http http://127.0.0.1:8000/mcp
```

## What it implements

| Verb | Behavior |
|---|---|
| POST | JSON-RPC request → single JSON response (`initialize` mints an `Mcp-Session-Id`); notifications → 202 |
| GET | Server-initiated SSE stream (one per session; a second returns 409) |
| DELETE | Explicit session termination |

Plus spec-mandated Origin validation (DNS-rebinding defense) and an
in-memory session store with idle eviction. Protocol handling — capabilities,
tool dispatch, versioning — stays entirely in the official SDK: this package
is transport only, so spec revisions ride SDK upgrades.

## Why

- Python is MCP's largest ecosystem, yet mounting an MCP endpoint inside your
  own web app still goes through ASGI plumbing with known friction.
- Cloudflare's remote-MCP story (Agents SDK, McpAgent) is TypeScript-only —
  MCP on Python Workers is unclaimed territory. hayate's SSE and Durable
  Object support are already verified on workerd, and the SDK itself imports
  and runs there (docs/research/pyodide.md).

## License

MIT
