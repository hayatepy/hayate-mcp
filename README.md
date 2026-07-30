# hayate-mcp

> **Hayate ecosystem:** [Start here](https://hayatepy.dev/)
> · [Production golden app](https://github.com/hayatepy/golden-app)
> · [Tested compatibility](https://hayatepy.dev/evidence/compatibility/)

Mount an MCP server into a [hayate](https://github.com/hayatepy/hayate) app:
an official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
bridge for ASGI and a focused, Pydantic-free tools runtime for Cloudflare
Python Workers, both over the same Streamable HTTP boundary.

> **Status: alpha (0.x).** Tracks the latest stable revision —
> **MCP 2026-07-28 and 2025-11-25 on both CPython/ASGI and Cloudflare Python
> Workers**. Modern clients use handshake-free `server/discover`, mandatory
> request metadata and routing headers, cache hints, `resultType`, MRTR, and
> stateless POSTs. Existing clients retain the 2025 initialize/session
> lifecycle. The internal
> design memo (Japanese) lives in [DESIGN.md](DESIGN.md); release history is
> in [CHANGELOG.md](CHANGELOG.md), and the 0.12 migration notes are in
> [docs/migration-0.12.md](docs/migration-0.12.md).

```python
import mcp.types as types
from hayate import Hayate
from mcp.server.lowlevel import Server
from hayate_mcp import McpMount


async def list_tools(_ctx, _params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def call_tool(_ctx, params):
    text = (params.arguments or {})["text"]
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server(
    "my-tools",
    version="1.0.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)
app = Hayate()
McpMount(server, path="/mcp").register(app)
```

Serve it with any ASGI server (`uvicorn server:app`), then connect:

```sh
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp --transport http --method tools/list
```

```sh
claude mcp add my-tools --transport http http://127.0.0.1:8000/mcp
```

## What it implements

| Era | ASGI | Workers |
|---|---|---|
| MCP 2026-07-28 | Handshake-free, stateless POST; JSON, response SSE, and live `subscriptions/listen` | Handshake-free, stateless POST; tools-only JSON |
| MCP 2025-11-25 | Stateful initialize/session lifecycle; JSON/SSE POST, optional GET SSE, DELETE | Stateless compatibility lifecycle and tools |

The modern path is always stateless, even when the same `McpMount` also serves
legacy stateful clients. Every request carries the protocol revision and
client capabilities in `_meta`, and the transport cross-checks
`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, and schema-declared
`Mcp-Param-*` headers before dispatch. Removed lifecycle methods return
JSON-RPC `-32601` with HTTP 404. Header, capability, and protocol-version
errors use the new `-32020`, `-32021`, and `-32022` codes with HTTP 400.

ASGI delegates discovery, capabilities, method dispatch, cache hints, and MRTR
result models to the official MCP Python SDK 2.x. Intermediate notifications
such as progress are returned before the final result on a bounded SSE
response stream. When an SDK server registers `subscriptions/listen`, Hayate
serves it as a response-owned live SSE stream with acknowledgment-first
ordering, subscription IDs, keepalives, proxy-buffer suppression, and
deterministic cancellation when the response body closes. The Workers runtime
implements the smaller tools capability it advertises and deliberately omits
resources, prompts, logging, sampling, Tasks, and subscription streams.

CI runs the pinned official MCP conformance runner against a comprehensive
SDK-backed fixture through this mount: **70/70 scenarios** — 30 for
2025-11-25 and all 40 current 2026-07-28 core scenarios. That includes
stateless discovery, result envelopes, caching, standard and custom headers,
JSON Schema 2020-12, HTTP error mapping, capability negotiation, and all MRTR
flows including multi-round encrypted `requestState` and tamper rejection.
The upstream legacy multiple-POST fixture still sends a non-negotiated version
([upstream #412](https://github.com/modelcontextprotocol/conformance/issues/412));
equivalent negotiated-version concurrency remains a local gate. Workers are
verified separately in current workerd and through the official SDK 2.x
client.

Browser requests are accepted automatically only when the endpoint and
`Origin` are both loopback (`localhost`, `127.0.0.1`, or `::1`). For every
other browser origin, pass the exact allowed origins with
`trusted_origins=[...]`. Non-browser clients such as the official SDK,
Inspector, and Claude Code do not send `Origin` and continue to work without
configuration. The request's reflected `Host` value is never treated as an
allow-list entry.

For 2025 clients, each stateful ASGI session stays pinned to the version
returned by `initialize`; a different later header returns 400. For 2026
clients, there is no initialize or session ID: the request metadata and HTTP
headers are the complete routing contract on every POST.

## Modern capabilities and MRTR

Declare client capabilities a tool actually needs. Hayate rejects the call
before the handler runs when the modern client did not opt in:

```python
McpMount(
    server,
    tool_capabilities={
        "draft_with_model": {"sampling": {}},
    },
).register(app)
```

The same contract is available on Workers as
`@server.tool(..., required_capabilities={"sampling": {}})`. Missing
capabilities return `-32021` with the structured `requiredCapabilities`
payload required by MCP.

SDK-backed servers can return `InputRequiredResult` from `tools/call`,
`prompts/get`, or `resources/read`. For low-level `Server` applications that
carry `requestState`, install the SDK's security boundary so clients receive
an authenticated, expiring token rather than application plaintext:

```python
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity

server.middleware.append(
    RequestStateBoundary(
        RequestStateSecurity(keys=[request_state_key]),
        default_audience=server.name,
    )
)
```

Use a shared secret from your deployment's secret store when requests can
land on multiple processes. Key rotation is supported by passing the active
key first and older verification keys after it. The complete multi-round
fixture is in [examples/conformance/server.py](examples/conformance/server.py).

## Authorization (OAuth 2.0 Resource Server)

Pass an `Authorization` to require Bearer tokens and serve RFC 9728 Protected
Resource Metadata (MCP Authorization, 2026-07-28 and 2025-11-25):

```python
from hayate_mcp import Authorization, McpMount

McpMount(
    server,
    authorization=Authorization(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://auth.example.com"],
        verify_token=verify,  # async (token) -> claims | None
        scopes_supported=["mcp", "documents:read"],
        required_scopes=["mcp"],
    ),
    tool_scopes={
        "read_document": ["documents:read"],
    },
).register(app)
```

Unauthenticated requests get `401` with
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`,
so clients (Claude, Inspector) discover the authorization server. Token
*issuance* is the AS's job — point `verify_token` at hayate-auth or any
OAuth 2.1 authorization server. The verifier must check signature, issuer,
expiry, and that the token audience/resource is this MCP server before it
returns claims; Hayate treats returned claims as that verification decision.

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

Insufficient global or per-tool scopes return 403 with a
`WWW-Authenticate` step-up challenge. Legacy stateful sessions are bound to
the creating `(issuer, client_id, subject)` identity; modern requests are
authenticated independently on every stateless POST.

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
server = WorkerMcpServer(
    "my-tools",
    version="1.0.0",
    description="Edge-native Python tools.",
    website_url="https://example.com",
    tools_ttl_ms=60_000,
    cache_scope="public",
)


@server.tool(
    name="echo",
    description="Echo text.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
async def echo(arguments):
    return f"echo: {arguments['text']}"


WorkerMcpMount(server).register(app)
Default = to_workers(app)
```

Tool input and structured output use JSON Schema 2020-12 and are validated
inside request scope, keeping workerd global initialization entropy-safe.
Modern discovery, required `resultType`, server identity metadata, cache
hints, routing headers, arbitrary JSON `structuredContent`, and optional
extension declarations use the same 2026 wire contract as ASGI.
Correctable validation failures and `ToolError` become model-visible `isError`
results, matching the official SDK. `WorkerProtocolError` preserves deliberate
JSON-RPC codes, HTTP statuses, and headers for request-aware edge authentication
or throttling; the modern protocol's mandatory HTTP mapping takes precedence
for reserved error codes. Unexpected exceptions are logged and sanitized before
reaching the model. OAuth and per-tool scopes use the same `Authorization` and
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
  hayate-mcp supplies a Python Workers path that speaks both current and
  compatibility MCP eras without requiring the Pydantic-based SDK in the edge
  bundle.

## License

MIT
