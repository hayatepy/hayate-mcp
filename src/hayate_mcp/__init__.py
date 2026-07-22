"""hayate-mcp: mount an MCP server on a hayate app."""

from .mount import McpMount
from .session import MemorySessionStore

__version__ = "0.1.0"

__all__ = ["McpMount", "MemorySessionStore", "__version__"]
