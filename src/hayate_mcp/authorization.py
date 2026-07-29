"""MCP Authorization: the OAuth 2.0 Resource Server side (DESIGN §5, v0.4).

Normative: MCP Authorization (2026-07-28 and 2025-11-25) + RFC 9728
(OAuth 2.0 Protected Resource Metadata) + RFC 6750 (Bearer). RFC 9449 DPoP
is available as an opt-in extension. An authorized MCP server:

- serves its Protected Resource Metadata at the RFC 9728 §3.1 well-known
  URI (``/.well-known/oauth-protected-resource`` with the resource's path
  inserted after it), naming the authorization server(s) a client should use;
- rejects unauthenticated requests with ``401`` and a ``WWW-Authenticate:
  Bearer resource_metadata="<that URL>"`` header, so the client can discover
  where to get a token (RFC 9728 §5.1).

Token *verification* is injected. Stable Bearer clients use
``verify_token(token) -> claims | None``. Sender-constrained deployments use
``verify_request(request) -> claims | None`` so the verifier can bind an
RFC 9449 proof to the method, URI, access token, key, and replay store. The
injected verifier is also responsible for issuer, expiry, signature, and
resource/audience validation; returning claims asserts all of those checks
have passed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from hayate import Request

from .principal import Principal

WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# verify_token: the raw Bearer credential -> claims dict if valid, else None.
VerifyToken = Callable[[str], Awaitable[dict[str, Any] | None]]
VerifyRequest = Callable[[Request], Awaitable[dict[str, Any] | None]]


@dataclass
class Authorization:
    resource: str
    authorization_servers: list[str]
    verify_token: VerifyToken | None = None
    scopes_supported: list[str] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    bearer_methods_supported: list[str] = field(default_factory=lambda: ["header"])
    verify_request: VerifyRequest | None = None
    authorization_scheme: Literal["Bearer", "DPoP"] = "Bearer"

    def __post_init__(self) -> None:
        if self.authorization_scheme not in ("Bearer", "DPoP"):
            raise ValueError("authorization_scheme must be 'Bearer' or 'DPoP'")
        if (self.verify_token is None) == (self.verify_request is None):
            raise ValueError("configure exactly one of verify_token or verify_request")
        if self.authorization_scheme == "DPoP" and self.verify_request is None:
            raise ValueError("DPoP requires a request-aware verify_request callable")
        resource = urlsplit(self.resource)
        try:
            _resource_port = resource.port
        except ValueError:
            raise ValueError("resource contains an invalid port") from None
        if (
            resource.scheme not in ("https", "http")
            or not resource.netloc
            or resource.fragment
            or resource.username
            or resource.password
        ):
            raise ValueError("resource must be an absolute HTTP(S) URI without a fragment")
        if resource.scheme == "http" and resource.hostname not in LOOPBACK_HOSTS:
            raise ValueError("resource must use https except on loopback hosts")
        self.resource = resource._replace(
            scheme=resource.scheme.lower(),
            netloc=resource.netloc.lower(),
        ).geturl()
        if not self.authorization_servers:
            raise ValueError("authorization_servers must contain at least one issuer")
        canonical_servers: list[str] = []
        for value in self.authorization_servers:
            issuer = urlsplit(value)
            try:
                _issuer_port = issuer.port
            except ValueError:
                raise ValueError("authorization server issuer contains an invalid port") from None
            if (
                issuer.scheme not in ("https", "http")
                or not issuer.netloc
                or issuer.query
                or issuer.fragment
                or issuer.username
                or issuer.password
            ):
                raise ValueError("authorization server issuers must be absolute HTTP(S) URIs")
            if issuer.scheme == "http" and issuer.hostname not in LOOPBACK_HOSTS:
                raise ValueError("authorization server issuers must use https except on loopback")
            canonical_servers.append(
                issuer._replace(
                    scheme=issuer.scheme.lower(),
                    netloc=issuer.netloc.lower(),
                ).geturl()
            )
        self.authorization_servers = canonical_servers
        if self.scopes_supported and not set(self.required_scopes) <= set(self.scopes_supported):
            raise ValueError("required_scopes must be included in scopes_supported")

    def metadata(self) -> dict[str, Any]:
        """The RFC 9728 Protected Resource Metadata document."""
        doc: dict[str, Any] = {
            "resource": self.resource,
            "authorization_servers": list(self.authorization_servers),
        }
        if self.authorization_scheme == "Bearer":
            doc["bearer_methods_supported"] = list(self.bearer_methods_supported)
        if self.scopes_supported:
            doc["scopes_supported"] = list(self.scopes_supported)
        return doc

    @property
    def metadata_url(self) -> str:
        """RFC 9728 §3.1: insert the well-known segment between host and the
        resource's path — ``https://h/mcp`` -> ``https://h{WELL_KNOWN_PRM}/mcp``.
        (Until 0.5.x this wrongly appended the segment after the path.)"""
        parts = urlsplit(self.resource)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        return f"{origin}{WELL_KNOWN_PRM}{path}"

    @property
    def metadata_path(self) -> str:
        """The path component of ``metadata_url`` (what a same-origin mount serves)."""
        return urlsplit(self.metadata_url).path

    def www_authenticate(
        self, error: str | None = None, *, scopes: list[str] | tuple[str, ...] = ()
    ) -> str:
        parts = [f'resource_metadata="{self.metadata_url}"']
        if error is not None:
            parts.insert(0, f'error="{error}"')
        if scopes:
            parts.append(f'scope="{" ".join(scopes)}"')
        return self.authorization_scheme + " " + ", ".join(parts)

    async def authenticate(self, authorization_header: str | None) -> Principal | None:
        """Verify a token-only authorization header (the stable Bearer path)."""
        if authorization_header is None or self.verify_token is None:
            return None
        scheme, _, credential = authorization_header.partition(" ")
        credential = credential.strip()
        if (
            scheme.lower() != self.authorization_scheme.lower()
            or not credential
            or any(character.isspace() for character in credential)
        ):
            return None
        claims = await self.verify_token(credential)
        return self._principal(claims)

    async def authenticate_request(self, request: Request) -> Principal | None:
        """Verify and normalize one complete immutable Fetch request."""
        raw = getattr(request, "raw", request)
        if self.verify_request is None:
            return await self.authenticate(raw.headers.get("authorization"))
        claims = await self.verify_request(raw)
        return self._principal(claims)

    @staticmethod
    def _principal(claims: dict[str, Any] | None) -> Principal | None:
        if claims is None:
            return None
        principal = dict(claims)
        subject = (
            principal.get("subject")
            or principal.get("sub")
            or principal.get("user_id")
            or principal.get("client_id")
        )
        if not isinstance(subject, str) or not subject:
            return None
        scopes = principal.get("scopes", principal.get("scope", []))
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            return None
        principal["subject"] = subject
        principal["scopes"] = scopes
        return principal

    def missing_scopes(
        self, principal: Principal, required: list[str] | tuple[str, ...] | None = None
    ) -> list[str]:
        expected = self.required_scopes if required is None else required
        granted = principal["scopes"]
        return [scope for scope in expected if scope not in granted]
