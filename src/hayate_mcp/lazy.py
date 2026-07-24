"""Workers-safe lazy registration without importing the MCP SDK globally."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from hayate import Context, Response

MountFactory = Callable[[Context], Any | Awaitable[Any]]


class LazyMcpMount:
    """Create and cache an ``McpMount`` on the first request.

    Keep MCP SDK imports inside ``factory``. This avoids workerd's global
    scope restrictions while replacing three hand-written transport routes.
    """

    def __init__(
        self,
        factory: MountFactory,
        *,
        path: str = "/mcp",
        metadata_path: str | None = None,
        cache: bool = True,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        if metadata_path is not None and not metadata_path.startswith("/"):
            raise ValueError("metadata_path must start with '/'")
        self.factory = factory
        self.path = path.rstrip("/") or "/"
        self.metadata_path = metadata_path
        self.cache = cache
        self._mount: Any | None = None
        self._lock: Any | None = None

    async def _create(self, c: Context) -> Any:
        mount = self.factory(c)
        if inspect.isawaitable(mount):
            mount = await mount
        if getattr(mount, "path", None) != self.path:
            raise ValueError(
                f"lazy mount path {self.path!r} does not match factory mount "
                f"path {getattr(mount, 'path', None)!r}"
            )
        return mount

    async def get(self, c: Context) -> Any:
        if not self.cache:
            return await self._create(c)
        if self._mount is not None:
            return self._mount
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        async with self._lock:
            if self._mount is None:
                self._mount = await self._create(c)
        return self._mount

    async def fetch(self, c: Context) -> Response:
        mount = await self.get(c)
        response = await mount.fetch(c.req.raw)
        if not isinstance(response, Response):
            raise TypeError("lazy MCP factory returned an invalid fetch handler")
        return response

    def register(self, app: Any) -> None:
        async def handler(c: Context) -> Response:
            return await self.fetch(c)

        for method in ("GET", "POST", "DELETE"):
            app.on(method, self.path)(handler)
        if self.metadata_path is not None:
            app.on("GET", self.metadata_path)(handler)
