# Migrating to hayate-mcp 0.12

Version 0.12 upgrades the CPython integration to MCP Python SDK 2.x and adds
the MCP 2026-07-28 protocol era. The endpoint remains compatible with
2025-11-25 clients; migration is primarily required for server definitions
that used SDK 1.x decorators.

## Dependency and server API

hayate-mcp now requires `mcp>=2.0.0,<3` outside Emscripten. Define low-level
handlers through the SDK v2 `Server` constructor:

```python
import mcp.types as types
from mcp.server.lowlevel import Server


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
    "example",
    version="1.0.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)
```

Result and request models use the SDK v2 fields (`params.input_responses`,
`params.request_state`, and `result_type`) and serialize their camel-case wire
aliases automatically.

## Protocol behavior

No application flag is needed to enable the modern protocol. `McpMount` and
`WorkerMcpMount` select the era from the request metadata and transport
headers.

- 2026-07-28 has no `initialize` or session ID. Every request is a stateless
  POST and must carry protocol version and client capabilities in `_meta`.
- 2025-11-25 keeps initialize/session behavior. Existing Inspector, Claude,
  and remote SDK v1 clients continue to use this path.
- Modern responses include `resultType` and server identity metadata.
  Cacheable SDK results should configure `cache_hints` on `Server`.
- Modern routing headers are mandatory and compared with the request body.
  Raw clients should normally use an SDK rather than construct them manually.

The old `stateless=True` option remains meaningful for 2025 clients. Modern
requests are stateless regardless of that setting.

## Subscription streams

SDK v2 replaces the old standalone GET stream with
`subscriptions/listen`. Low-level servers opt in by registering the SDK's
`ListenHandler`; `MCPServer` does this automatically:

```python
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler

subscription_bus = InMemorySubscriptionBus()
server = Server(
    "example",
    on_subscriptions_listen=ListenHandler(subscription_bus),
    # ...other handlers...
)
```

Hayate detects the registered method and returns a live, response-owned SSE
stream. Do not also advertise list-change or resource-subscription
capabilities through a separate mechanism without registering the listener.
Workers remain deliberately tools-only and do not advertise subscriptions.

## Required client capabilities

If a tool requires sampling, elicitation, roots, or an extension, declare the
requirement at the mount boundary:

```python
McpMount(
    server,
    tool_capabilities={
        "draft_with_model": {"sampling": {}},
        "async_export": {
            "extensions": {
                "io.modelcontextprotocol/tasks": {},
            }
        },
    },
).register(app)
```

Workers tools use the equivalent decorator argument:

```python
@server.tool(
    name="draft_with_model",
    input_schema={"type": "object", "properties": {}},
    required_capabilities={"sampling": {}},
)
async def draft_with_model(arguments): ...
```

An undeclared requirement is rejected with `-32021` and HTTP 400 before the
handler executes.

## MRTR and requestState

SDK-backed `tools/call`, `prompts/get`, and `resources/read` handlers may
return `types.InputRequiredResult`. A low-level server that returns
`requestState` should install `RequestStateBoundary`:

```python
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity

server.middleware.append(
    RequestStateBoundary(
        RequestStateSecurity(keys=[request_state_key]),
        default_audience=server.name,
    )
)
```

Do not use the conformance fixture's static test key in production. Use at
least one deployment secret shared by every process that may receive the next
round. Rotate without downtime by placing the new signing key first and
retaining older verification keys for at least the configured TTL.

## Tasks

Tasks are no longer a core capability in 2026-07-28. They live under the
`io.modelcontextprotocol/tasks` extension and require explicit client
negotiation. hayate-mcp does not advertise this extension by default. Do not
carry a legacy `capabilities.tasks` declaration into a modern server.

## Verification

Run the normal quality gates and the dual-era official conformance suite:

```sh
uv run pytest
uv run mypy src
uv run ruff check .
bash scripts/check_conformance.sh
```

The conformance script runs 30 legacy and 40 modern core scenarios without an
expected-failure baseline.
