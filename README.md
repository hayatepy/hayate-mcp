# hayate-mcp

Mount an MCP server into a [hayate](https://github.com/hayatepy/hayate) app —
a Streamable HTTP transport that bridges the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) to
WHATWG Request/Response. The [@hono/mcp](https://www.npmjs.com/package/@hono/mcp)
architecture, in Python.

> **Status: design phase.** Nothing installable yet. The internal design memo
> (Japanese, per project convention) lives in [DESIGN.md](DESIGN.md).

## Why

- Python is MCP's largest ecosystem, yet mounting an MCP endpoint inside your
  own web app still goes through ASGI plumbing with known friction.
- Cloudflare's remote-MCP story (Agents SDK, McpAgent) is TypeScript-only —
  MCP on Python Workers is unclaimed territory. hayate's SSE and Durable
  Object support are already verified on workerd.
- This package implements *only* the transport (POST/GET/DELETE + SSE +
  `Mcp-Session-Id` + Origin validation, per the MCP spec). Tools are defined
  with the official SDK; the protocol itself is never reimplemented.

## Planned shape

```python
from mcp.server import Server      # official SDK — define your tools here
from hayate import Hayate
from hayate_mcp import McpMount

server = Server("my-tools")

app = Hayate()
McpMount(server, path="/mcp").register(app)
```

Same code on uvicorn and Cloudflare Python Workers; sessions back onto a
Durable Object on Workers. OAuth via
[hayate-auth](https://github.com/hayatepy/hayate-auth) is on the roadmap.

## License

MIT
