# hayate-mcp

Mount an MCP server into a [hayate](https://github.com/hayatepy/hayate) app —
a Streamable HTTP transport that bridges the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) to
WHATWG Request/Response. The [@hono/mcp](https://www.npmjs.com/package/@hono/mcp)
architecture, in Python.

> **Status: alpha (0.6.x).** Tracks the SDK's latest revision — **2025-11-25**
> on CPython/ASGI (mcp ≥ 1.28), with `MCP-Protocol-Version` header validation.
> Serves MCP Inspector, Claude Code, and the official SDK client — single-JSON
> POST plus the optional server-initiated GET SSE stream on ASGI, and a
> **stateless mode that runs on Cloudflare Workers** (verified on workerd and
> a deployed Workers Paid application).
> On Workers the SDK is currently pinned to a 2025-06-18-capable version by
> Pyodide's `pydantic-core` wheel availability (DESIGN §6.2). The internal
> design memo (Japanese) lives in [DESIGN.md](DESIGN.md); release history is
> in [CHANGELOG.md](CHANGELOG.md).

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

## Authorization (OAuth 2.0 Resource Server)

Pass an `Authorization` to require Bearer tokens and serve RFC 9728 Protected
Resource Metadata (MCP Authorization, 2025-06-18):

```python
from hayate_mcp import Authorization, McpMount

McpMount(server, authorization=Authorization(
    resource="https://mcp.example.com",
    authorization_servers=["https://auth.example.com"],
    verify_token=verify,   # async (token) -> claims | None
)).register(app)
```

Unauthenticated requests get `401` with
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`,
so clients (Claude, Inspector) discover the authorization server. Token
*issuance* is the AS's job — point `verify_token` at hayate-auth or any
RFC 6749 server.

## On Cloudflare Workers

Pass `stateless=True` and mount on a plain Worker — no Durable Object needed.
Each request runs the SDK server to completion on its own, which is what makes
it Workers-safe:

```python
from hayate import Hayate
from hayate.adapters.workers import to_workers
from hayate_mcp import McpMount

app = Hayate()
McpMount(build_server(), stateless=True).register(app)
Default = to_workers(app)
```

See [examples/workers](examples/workers) (verified on workerd). Trade-off:
stateless has no server-initiated GET stream and no cross-request session
state — use the default stateful mode on ASGI ([examples/echo](examples/echo))
when you need those. (Import `mcp` lazily inside a handler on Workers, never
at global scope — its dependency chain seeds entropy at import.)

Production verification uses a Workers Paid account: the cold lazy import of
the MCP dependency chain takes roughly 3 seconds of CPU and exceeds the Free
plan's Python runtime limiter. The application itself fits the Free plan's
compressed bundle-size limit, but the SDK import does not complete there.
Deployment measurements and the account/plan traps are recorded in
[docs/research/pyodide.md](docs/research/pyodide.md).

## Why

- Python is MCP's largest ecosystem, yet mounting an MCP endpoint inside your
  own web app still goes through ASGI plumbing with known friction.
- Cloudflare's remote-MCP story (Agents SDK, McpAgent) is TypeScript-only —
  MCP on Python Workers was unclaimed territory. hayate-mcp runs there today
  (stateless mode, verified locally and on Workers Paid); the SDK imports and runs on Pyodide
  (docs/research/pyodide.md).

## License

MIT
