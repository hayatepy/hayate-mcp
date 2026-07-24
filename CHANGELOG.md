# Changelog

All notable changes to hayate-mcp are documented here.

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
