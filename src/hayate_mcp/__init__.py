"""hayate-mcp: mount an MCP server on a hayate app.

Top-level names are resolved lazily (PEP 562) so that ``import hayate_mcp``
on Cloudflare Workers does not eagerly pull in the ``mcp`` SDK. The SDK's
transitive dependency ``rpds`` seeds entropy at import via
``getRandomValues``, which workerd forbids during global-scope evaluation;
deferring keeps a Workers entry module importable at global scope while
CPython users still write ``from hayate_mcp import McpMount``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.8.0"

__all__ = [
    "Authorization",
    "LazyMcpMount",
    "McpMount",
    "MemorySessionStore",
    "ToolError",
    "WorkerMcpMount",
    "WorkerMcpServer",
    "WorkerTool",
    "__version__",
    "get_principal",
]

if TYPE_CHECKING:
    from .authorization import Authorization
    from .lazy import LazyMcpMount
    from .mount import McpMount
    from .principal import get_principal
    from .session import MemorySessionStore
    from .worker import ToolError, WorkerMcpMount, WorkerMcpServer, WorkerTool


def __getattr__(name: str) -> Any:
    if name == "McpMount":
        from .mount import McpMount

        return McpMount
    if name == "MemorySessionStore":
        from .session import MemorySessionStore

        return MemorySessionStore
    if name == "Authorization":
        from .authorization import Authorization

        return Authorization
    if name == "LazyMcpMount":
        from .lazy import LazyMcpMount

        return LazyMcpMount
    if name == "get_principal":
        from .principal import get_principal

        return get_principal
    if name in {"ToolError", "WorkerMcpMount", "WorkerMcpServer", "WorkerTool"}:
        from .worker import ToolError, WorkerMcpMount, WorkerMcpServer, WorkerTool

        return {
            "ToolError": ToolError,
            "WorkerMcpMount": WorkerMcpMount,
            "WorkerMcpServer": WorkerMcpServer,
            "WorkerTool": WorkerTool,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
