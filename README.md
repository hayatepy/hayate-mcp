# hayate-mcp

> **Hayate ecosystem:** [Start here](https://github.com/hayatepy/.github/blob/main/docs/START.md)
> · [Production golden app](https://github.com/hayatepy/golden-app)
> · [Tested compatibility](https://github.com/hayatepy/.github/blob/main/docs/COMPATIBILITY.md)

Mount an MCP server into a [hayate](https://github.com/hayatepy/hayate) app:
an official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
bridge for ASGI and a focused, Pydantic-free tools runtime for Cloudflare
Python Workers, both over the same Streamable HTTP boundary.

> **Status: alpha (0.10.x).** Tracks the latest stable revision —
> **MCP 2025-11-25 on both CPython/ASGI and Cloudflare Python Workers**, with
> `MCP-Protocol-Version` header validation.
> Serves MCP Inspector, Claude Code, and the official SDK client — single-JSON
> POST plus the optional server-initiated GET SSE stream on ASGI, and a
> **stateless tools runtime on Cloudflare Workers**, verified in CI on workerd
> and by the official SDK client. The internal
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

| Verb | ASGI | Workers |
|---|---|---|
| POST | JSON-RPC → JSON/SSE; stateful `initialize` mints an `Mcp-Session-Id` | JSON-RPC → single JSON; stateless |
| GET | Server-initiated SSE stream (one per session; a second returns 409) | 405 (not advertised) |
| DELETE | Explicit session termination | 200 stateless no-op |

POST requests must use `Content-Type: application/json` and advertise both
`application/json` and `text/event-stream` in `Accept`; notifications and
client responses receive an empty 202. GET must advertise SSE. Plus
spec-mandated Origin validation (DNS-rebinding defense) and an
in-memory session store with idle eviction. On ASGI, capabilities, dispatch,
and versioning stay in the official SDK. The Workers runtime implements only
the lifecycle, ping, and tools surface it advertises; resources, prompts,
logging, sampling, tasks, and server-initiated streams are omitted from
capabilities rather than partially implemented.

CI runs the pinned official MCP conformance runner against a comprehensive
SDK-backed fixture through this mount. The suite covers 30 current 2025-11-25
server scenarios that apply without the optional resumability extension,
including lifecycle, Streamable HTTP, every SDK capability, JSON Schema
2020-12 preservation, and DNS-rebinding protection. The remaining official
multiple-POST scenario currently sends a non-negotiated protocol version
([upstream #412](https://github.com/modelcontextprotocol/conformance/issues/412));
Hayate rejects that mismatch and runs equivalent three-request concurrency
coverage with the negotiated version locally. Workers keep their smaller
advertised tools-only denominator and are verified separately through workerd
with the official SDK client.

Browser requests are accepted automatically only when the endpoint and
`Origin` are both loopback (`localhost`, `127.0.0.1`, or `::1`). For every
other browser origin, pass the exact allowed origins with
`trusted_origins=[...]`. Non-browser clients such as the official SDK,
Inspector, and Claude Code do not send `Origin` and continue to work without
configuration. The request's reflected `Host` value is never treated as an
allow-list entry.

After `initialize`, clients send `MCP-Protocol-Version` on every subsequent
HTTP request. Each stateful ASGI session is pinned to the version returned by
its successful initialize response; a different version returns 400 even when
the SDK supports that revision for other sessions. If the header is missing,
ASGI uses the version already bound to the session as the transport's
backwards-compatible source of truth. The stateless Workers runtime accepts
exactly `2025-11-25`; because it does not retain a session and does not
implement the older fallback revision, a missing or different header returns
400.

## Authorization (OAuth 2.0 Resource Server)

Pass an `Authorization` to require Bearer tokens and serve RFC 9728 Protected
Resource Metadata (MCP Authorization, 2025-11-25):

```python
from hayate_mcp import Authorization, McpMount

McpMount(server, authorization=Authorization(
    resource="https://mcp.example.com/mcp",
    authorization_servers=["https://auth.example.com"],
    verify_token=verify,   # async (token) -> claims | None
    scopes_supported=["mcp", "documents:read"],
    required_scopes=["mcp"],
), tool_scopes={
    "read_document": ["documents:read"],
}).register(app)
```

Unauthenticated requests get `401` with
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`,
so clients (Claude, Inspector) discover the authorization server. Token
*issuance* is the AS's job — point `verify_token` at hayate-auth or any
RFC 6749 server.

Verified claims are normalized (`subject`, `client_id`, `scopes`) and are
available inside tool handlers:

```python
from hayate_mcp import get_principal

@server.call_tool()
async def call_tool(name, arguments):
    principal = get_principal()
    assert principal is not None
    # principal["subject"], principal["scopes"], ...
```

Insufficient global or per-tool scopes return 403 with the MCP 2025-11-25
`WWW-Authenticate` step-up challenge. Stateful sessions are bound to the
creating `(issuer, client_id, subject)` identity.

RFC 9449 DPoP is available without changing the stable Bearer default. A
sender-constrained verifier needs the complete request, not just the opaque
token:

```python
from hayate_mcp import Authorization

authorization = Authorization(
    resource="https://folio.example/mcp",
    authorization_servers=["https://auth.example"],
    verify_request=dpop_request_verifier,  # async (Request) -> claims | None
    authorization_scheme="DPoP",
    scopes_supported=["mcp"],
    required_scopes=["mcp"],
)
```

Both `McpMount` and `WorkerMcpMount` pass the immutable Fetch `Request` to the
verifier, allowing it to validate the proof signature, `htm`, `htu`, `ath`,
token `cnf.jkt`, and replay state. hayate-auth provides a compatible
`DPoPRequestVerifier`. Current official MCP SDK OAuth clients model Bearer
tokens only, so DPoP remains an explicit client/server extension until SDK
support lands.

## Hayate request context

Tools mounted with `register(app)` can reuse request-scoped Hayate state,
headers, and runtime bindings without adding them to model-visible arguments:

```python
from hayate_mcp import get_request_context

context = get_request_context()
assert context is not None
database = context.env.DB
request_id = context.get("request_id")
```

The context is isolated across concurrent requests and reset when each request
finishes. `get_request_context()` returns `None` outside a registered mount;
the lower-level `mount.fetch(request)` API deliberately has no app context.

## On Cloudflare Workers

Use `WorkerMcpServer` and `WorkerMcpMount` on a plain Worker — no Durable
Object, Pydantic, or old SDK line is needed:

```python
from hayate import Hayate
from hayate.adapters.workers import to_workers
from hayate_mcp import WorkerMcpMount, WorkerMcpServer

app = Hayate()
server = WorkerMcpServer("my-tools", version="1.0.0")

@server.tool(
    name="echo",
    description="Echo text.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    execution={"taskSupport": "forbidden"},
)
async def echo(arguments):
    return f"echo: {arguments['text']}"

WorkerMcpMount(server).register(app)
Default = to_workers(app)
```

Tool input and structured output use JSON Schema 2020-12 and are validated
inside request scope, keeping workerd global initialization entropy-safe.
Correctable validation failures and `ToolError` become model-visible `isError`
results, matching the official SDK. `WorkerProtocolError` preserves deliberate
JSON-RPC codes, HTTP statuses, and headers for request-aware edge authentication
or throttling; unexpected exceptions are logged and sanitized before reaching
the model. OAuth and per-tool scopes use the same `Authorization` and
`get_principal()` APIs as the SDK-backed mount.

See [examples/workers](examples/workers). The Workers surface is stateless and
does not advertise server-initiated streams or session state. Use the default
SDK-backed ASGI mount ([examples/echo](examples/echo)) when you need those.
CI builds the local wheel in an isolated project, boots current workerd, and
connects with the official MCP SDK client.

## Why

- Python is MCP's largest ecosystem, yet mounting an MCP endpoint inside your
  own web app still goes through ASGI plumbing with known friction.
- Cloudflare's remote-MCP story (Agents SDK, McpAgent) is TypeScript-first.
  hayate-mcp supplies a Python Workers path that speaks the same stable MCP
  revision without requiring the Pydantic-based SDK in the edge bundle.

## License

MIT
