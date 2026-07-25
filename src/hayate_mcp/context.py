"""Hayate request context propagated into MCP handlers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from hayate import Context

_current_context: ContextVar[Context | None] = ContextVar(
    "hayate_mcp_current_context",
    default=None,
)


def get_request_context() -> Context | None:
    """Return the active Hayate context when the mount was registered on an app."""
    return _current_context.get()


@contextmanager
def request_context(context: Context) -> Iterator[None]:
    token = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)
