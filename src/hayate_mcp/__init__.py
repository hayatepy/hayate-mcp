"""hayate-mcp: mount an MCP server on a hayate app.

Top-level names are resolved lazily (PEP 562) so that ``import hayate_mcp``
on Cloudflare Workers does not eagerly pull in the ``mcp`` SDK. The SDK's
transitive dependency ``rpds`` seeds entropy at import via
``getRandomValues``, which workerd forbids during global-scope evaluation;
deferring keeps a Workers entry module importable at global scope while
CPython users still write ``from hayate_mcp import McpMount``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.5.0"

__all__ = ["Authorization", "McpMount", "MemorySessionStore", "__version__"]

if TYPE_CHECKING:
    from .authorization import Authorization
    from .mount import McpMount
    from .session import MemorySessionStore


def __getattr__(name: str):
    if name == "McpMount":
        from .mount import McpMount

        return McpMount
    if name == "MemorySessionStore":
        from .session import MemorySessionStore

        return MemorySessionStore
    if name == "Authorization":
        from .authorization import Authorization

        return Authorization
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
