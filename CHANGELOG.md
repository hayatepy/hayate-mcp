# Changelog

All notable changes to hayate-mcp are documented here.

## Unreleased

### Fixed

- Bind every stateful ASGI session to the protocol version returned by its
  successful initialize response, reject a different revision on later POST,
  GET, and DELETE requests, and remove sessions whose initialization fails.
- Refresh idle-session activity only after ownership, negotiated-version, and
  request authorization checks pass, so rejected requests cannot keep another
  session alive or influence eviction order.

### Changed

- Keep strict negotiated-version enforcement when the pinned conformance
  runner sends a stale revision in its multiple-POST scenario
  ([upstream #412](https://github.com/modelcontextprotocol/conformance/issues/412));
  cover the same three concurrent requests locally with the negotiated
  revision while the other 30 official scenarios remain in the runner gate.

## [0.11.0] - 2026-07-27

### Added

- Add an opt-in request-aware authorization verifier and DPoP challenge mode
  to both ASGI and Workers mounts, allowing RFC 9449 method, URI, access-token,
  key-binding, and replay validation while preserving the stable Bearer API.

## [0.10.1] - 2026-07-26

### Security

- Reject DNS-rebinding requests whose attacker-controlled `Host` and `Origin`
  reflect each other. Browser origins outside loopback now require an explicit
  `trusted_origins` entry on both ASGI and Workers transports.

### Added

- Run 31 applicable MCP 2025-11-25 server scenarios from the pinned official
  conformance runner in CI, without expected-failure baselines.

### Changed

- Link the canonical ecosystem start page, production golden app, and tested
  compatibility evidence from the published package description.

## [0.10.0] - 2026-07-25

### Added

- Add MCP 2025-11-25 tool `_meta` and `execution` fields to the Workers API,
  while rejecting task-support claims the stateless runtime does not
  advertise.
- Add `WorkerProtocolError` so request-aware tools can preserve deliberate
  JSON-RPC codes, HTTP statuses, and headers for edge authentication,
  throttling, and unavailable dependencies.

### Changed

- Return JSON Schema input failures as model-visible `isError` tool results,
  matching the official SDK and the MCP guidance for correctable input errors.

## [0.9.0] - 2026-07-25

### Added

- Add `get_request_context()` for tools mounted with `register(app)`, allowing
  both SDK-backed and Workers handlers to reuse Hayate headers, request state,
  and runtime bindings without exposing infrastructure as tool arguments.
- Isolate the propagated context across concurrent requests and reset it after
  each request.

### Changed

- Make the workerd gate install the built wheel and its Workers-only
  dependencies into pywrangler's Pyodide environment, then assert that the
  required vendored packages are present.
- Keep the gate compatible with current Node releases by removing Pyodide's
  obsolete `--experimental-wasm-stack-switching` launcher argument.

## [0.8.0] - 2026-07-24

### Added

- Add a Pydantic-free `WorkerMcpServer` and `WorkerMcpMount` implementing the
  negotiated MCP 2025-11-25 lifecycle, ping, and tools capability on
  Cloudflare Python Workers.
- Add Draft 2020-12 input/output schema validation, structured tool results,
  sanitized failures, OAuth principals, and per-tool scopes to the Workers
  runtime.
- Add a workerd CI gate that builds the local wheel, verifies the dependency
  bundle contains neither the official SDK nor Pydantic, and runs the official
  MCP SDK client through initialize, tools/list, and tools/call.

### Changed

- Remove the Emscripten dependency on the old MCP SDK line. Both ASGI and
  Workers now negotiate the latest stable protocol revision, 2025-11-25.
- Replace the Workers example's request-scoped SDK import with an entropy-safe
  static tools server whose production bundle is substantially smaller.
- Fuzz arbitrary JSON values through the Workers transport and require every
  successful response to validate against the official SDK's JSON-RPC model.
- Audit locked dependencies on every change and publish an SPDX SBOM plus
  GitHub build and SBOM attestations with each release.

## [0.7.0] - 2026-07-24

### Added

- Add normalized authenticated principals, `get_principal()` propagation into
  tool handlers, mount-wide required scopes, and per-tool scope challenges.
- Add `LazyMcpMount` for request-aware Workers factories without importing the
  official SDK at module initialization time.
- Add strict typing metadata and mypy validation.

### Changed

- Align Streamable HTTP with MCP 2025-11-25: POST requires both advertised
  response media types and JSON input, notifications and client responses
  return empty 202 responses, GET requires SSE, and unsupported protocol
  versions return 400.
- Bind stateful sessions to the authenticated issuer, client, and subject;
  another principal receives 404 rather than access to the session.
- Validate and canonicalize protected-resource and authorization-server URIs,
  requiring HTTPS outside loopback development.
- Require official SDK 1.28.1 or newer on CPython and cap it below the
  forthcoming incompatible v2 line. Emscripten keeps the compatible 1.x floor
  until Pyodide supplies the required `pydantic-core` wheel.

## [0.6.1] - 2026-07-24

### Changed

- Document the current 0.6 protocol line and the verified production
  Cloudflare Workers deployment constraints.
- Add a complete public release history and current documentation links.
- Harden releases with protected tag-only publishing, tag/version validation,
  and automatic GitHub Release creation after PyPI succeeds.

## [0.6.0] - 2026-07-23

### Fixed

- Serve RFC 9728 Protected Resource Metadata at the path-insertion URI that is
  advertised to clients.

## [0.5.0] - 2026-07-23

### Added

- MCP 2025-11-25 support on CPython through the current official SDK.
- `MCP-Protocol-Version` validation against the SDK's supported revisions.

## [0.4.0] - 2026-07-23

### Added

- OAuth 2.0 resource-server support: RFC 9728 metadata, Bearer verification,
  and discoverable `401` responses.

## [0.3.0] - 2026-07-23

### Added

- Stateless per-request mode for Cloudflare Python Workers without a Durable
  Object.

## [0.2.0] - 2026-07-23

### Added

- Server-initiated GET SSE streams for stateful ASGI sessions.

## [0.1.0] - 2026-07-22

### Added

- Streamable HTTP POST and DELETE transport, sessions, Origin validation, and
  an official SDK client E2E test over real HTTP.
