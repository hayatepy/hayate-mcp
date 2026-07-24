"""Authenticated principal propagated into MCP request handlers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

type Principal = dict[str, Any]
type PrincipalIdentity = tuple[str | None, str | None, str]

_current_principal: ContextVar[Principal | None] = ContextVar(
    "hayate_mcp_current_principal", default=None
)


def get_principal() -> Principal | None:
    """Return the verified principal for the active MCP request, if any."""
    return _current_principal.get()


def principal_identity(principal: Principal | None) -> PrincipalIdentity | None:
    """Match the Python SDK's session-owner tuple: issuer, client, subject."""
    if principal is None:
        return None
    issuer = principal.get("iss")
    client_id = principal.get("client_id")
    return (
        str(issuer) if issuer is not None else None,
        str(client_id) if client_id is not None else None,
        principal["subject"],
    )


@contextmanager
def principal_context(principal: Principal | None) -> Iterator[None]:
    token = _current_principal.set(principal)
    try:
        yield
    finally:
        _current_principal.reset(token)
