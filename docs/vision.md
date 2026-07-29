# hayate-mcp protocol vision

hayate-mcp should be the smallest trustworthy boundary between Python
application code and the MCP network protocol, from a conventional ASGI
process to a Cloudflare isolate. Its advantage is not another tool-definition
DSL. It is one portable Fetch-shaped transport, strict capability honesty,
and evidence that the same contract works at both origins and the edge.

## Product principles

### Protocol eras are explicit

Current and compatibility clients may share one URL, but they must never
share ambiguous state. Era selection is deterministic, modern requests are
self-contained, and legacy sessions retain exactly one negotiated revision.
Future revisions should add a new isolated routing ladder before removing an
old one.

### Advertised capability equals tested behavior

An optional capability or extension is not exposed until negotiation,
positive behavior, negative behavior, and conformance coverage ship together.
This keeps the Workers surface small without making it second-class: a
tools-only server that implements every advertised requirement is more useful
than a broad server with partial semantics.

### State crosses the network only with integrity

MRTR makes stateless workflows possible without server affinity.
`requestState` must therefore be treated as a security token: encrypted or
authenticated, expiring, bound to the method and arguments, scoped to the
service audience, and optionally bound to the authenticated principal.
Plain application state must not be exposed as the wire token.

### Edge-native is the default pressure test

No design should require an ASGI server merely because Python historically
used ASGI. The transport core stays free of the official SDK so it can run in
Pyodide. SDK-backed CPython and Workers-native dispatch share validation and
error semantics, while each runtime only pays for the capabilities it uses.

### Claims require executable evidence

Protocol support is measured by pinned official scenarios, SDK
interoperability, workerd execution, and negative security tests. README
claims must name their denominator. New specification work is incomplete
until it is reproducible in CI.

## Forward path

### 1. Extension-safe Tasks

Tasks should be implemented as an optional `io.modelcontextprotocol/tasks`
module, not folded back into the core runtime. It needs per-request client
opt-in, durable task storage, cancellation, expiry, notifications, and the
MRTR-to-task composition tests before advertisement. Durable Objects are a
natural Workers backend, but the task protocol must not depend on them.

### 2. Portable request-state key management

Expose a small configuration layer for shared keys, rotation rings, TTL, and
KMS-backed codecs without replacing the SDK security primitive. Provide
deployment recipes for a single process, multi-worker ASGI, and Cloudflare
Secrets. Fail closed when state cannot be verified.

### 3. Protocol-aware observability

Add OpenTelemetry hooks at the transport boundary with stable attributes for
protocol era, method, tool name, result type, cache scope, HTTP status, MCP
error code, and capability rejection. Never record tokens, routed parameter
headers, tool arguments, or requestState by default.

### 4. Conformance matrix as a published artifact

Publish machine-readable results for ASGI and Workers, separated into core
and negotiated extensions. Track upstream conformance versions exactly and
make specification drift visible before a release rather than after users
upgrade.

### 5. Edge MRTR

Allow Workers tools to return state-free `InputRequiredResult` first, then add
secure requestState once a WebCrypto-compatible codec and multi-isolate key
story are proven in workerd. Do not silently emit unprotected continuation
state as an interim shortcut.

### 6. Policy composition with the Hayate ecosystem

Compose MCP client capabilities, OAuth scopes, principal identity, rate
limits, and host request context at one pre-dispatch policy boundary. FolioMCP
is the design partner workload: recurring friction there should become
generic, documented primitives here rather than application-specific hooks.

## Release bar

A stable 1.0 should require:

- a frozen public mount and Workers registration API;
- two supported protocol eras with a documented retirement policy;
- green official core conformance on every advertised runtime;
- authenticated MRTR guidance and rotation tests;
- reproducible workerd and real HTTP SDK interoperability;
- a security policy, compatibility table, and machine-readable conformance
  evidence published with each release.
