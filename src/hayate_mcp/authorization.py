"""MCP Authorization: the OAuth 2.0 Resource Server side (DESIGN §5, v0.4).

Normative: MCP Authorization (2025-06-18) + RFC 9728 (OAuth 2.0 Protected
Resource Metadata) + RFC 6750 (Bearer). An authorized MCP server:

- serves its Protected Resource Metadata at
  ``/.well-known/oauth-protected-resource`` (RFC 9728), naming the
  authorization server(s) a client should use;
- rejects unauthenticated requests with ``401`` and a ``WWW-Authenticate:
  Bearer resource_metadata="<that URL>"`` header, so the client can discover
  where to get a token (RFC 9728 §5.1).

Token *verification* is injected — ``verify_token(token) -> claims | None`` —
so the authorization server (hayate-auth, or any RFC 6749 AS) stays a
separate concern. This is the resource-server half of the "MCP server + its
AS in one app" story; the AS half lives in hayate-auth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"

# verify_token: the raw Bearer credential -> claims dict if valid, else None.
VerifyToken = Callable[[str], Awaitable[dict[str, Any] | None]]


@dataclass
class Authorization:
    resource: str
    authorization_servers: list[str]
    verify_token: VerifyToken
    scopes_supported: list[str] = field(default_factory=list)
    bearer_methods_supported: list[str] = field(default_factory=lambda: ["header"])

    def metadata(self) -> dict[str, Any]:
        """The RFC 9728 Protected Resource Metadata document."""
        doc: dict[str, Any] = {
            "resource": self.resource,
            "authorization_servers": list(self.authorization_servers),
            "bearer_methods_supported": list(self.bearer_methods_supported),
        }
        if self.scopes_supported:
            doc["scopes_supported"] = list(self.scopes_supported)
        return doc

    @property
    def metadata_url(self) -> str:
        return self.resource.rstrip("/") + WELL_KNOWN_PRM

    def www_authenticate(self, error: str | None = None) -> str:
        parts = [f'resource_metadata="{self.metadata_url}"']
        if error is not None:
            parts.insert(0, f'error="{error}"')
        return "Bearer " + ", ".join(parts)

    async def authenticate(self, authorization_header: str | None) -> dict[str, Any] | None:
        """Return verified claims, or None if the credential is absent/invalid."""
        if authorization_header is None:
            return None
        scheme, _, credential = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return None
        return await self.verify_token(credential.strip())
