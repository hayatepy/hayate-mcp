"""Workers Durable Object session store (DESIGN §4).

An McpSession holds a *live* anyio task (the running SDK Server), so it
cannot be serialized into KV or D1 — it needs a home that stays warm and
single-threaded per session. That is exactly a Durable Object: route each
session to its own object, and inside that object an ordinary ``McpMount``
runs with the in-memory store. Cloudflare's own McpAgent uses the same shape.

The session id *is* the Durable Object's own id string (``ctx.id``):
initialize routes to a fresh object which reports its id as the
``Mcp-Session-Id``, and later requests reconstruct that object with
``idFromString``. No id injection is needed: the outer app forwards the
original platform request via ``forward()`` (verified to carry POST bodies
to a Durable Object), and the object's streaming response — single JSON or
SSE — passes back through untouched.

Note: ``mcp`` is imported lazily inside the functions here, never at module
scope. On workerd the SDK's import chain (jsonschema/rpds) seeds entropy at
import via ``getRandomValues``, which workerd forbids during Worker
*global-scope* evaluation **and object construction**; deferring the import
to first request (an allowed scope) is the same discipline the on-workerd
spike established, and why the package ``__init__`` is lazy too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hayate import Context, Hayate, Response

_SESSION_HEADER = "mcp-session-id"


def mcp_durable_object(
    build_server: Callable[[], Any],
    *,
    path: str = "/mcp",
    trusted_origins: tuple[str, ...] | list[str] = (),
    class_name: str = "McpSession",
) -> Callable[[Any, Any], Hayate]:
    """A ``(ctx, env) -> Hayate`` factory to wrap with ``to_durable_object``.

    The mount pins its session id to this object's own ``ctx.id`` string, so
    the id the client receives on initialize routes straight back here.

    ``class_name`` becomes the factory's ``__name__``; ``to_durable_object``
    registers the Durable Object class under it, so it must match
    ``class_name`` in wrangler.toml (workerd looks classes up by name)."""

    def factory(ctx: Any, env: Any) -> Hayate:
        do_app = Hayate()
        state: dict[str, Any] = {}

        async def handle(c: Context) -> Response:
            # mcp (jsonschema/rpds) seeds entropy at import, which workerd
            # forbids during object *construction* as well as global scope —
            # so build the server, store, and mount on first request (an
            # allowed scope) and cache them for the object's lifetime. The
            # session id is this object's own id, so the id the client gets
            # on initialize routes back here via idFromString().
            if "mount" not in state:
                from .mount import McpMount
                from .session import MemorySessionStore

                state["mount"] = McpMount(
                    build_server(),
                    path=path,
                    trusted_origins=trusted_origins,
                    store=MemorySessionStore(),
                    session_id=ctx.id.toString(),
                )
            return await state["mount"].fetch(c.req)

        for method in ("GET", "POST", "DELETE"):
            do_app.on(method, path)(handle)
        return do_app

    factory.__name__ = class_name
    factory.__qualname__ = class_name
    return factory


async def route_to_session(c: Context, binding: Any) -> Response:
    """Forward an MCP request to its session's Durable Object.

    Initialize (no ``Mcp-Session-Id``) goes to a fresh unique object, which
    reports its own id as the session id; later requests reconstruct that
    object with ``idFromString``. ``forward()`` re-sends the original platform
    request — verified to carry POST bodies to a Durable Object — so the
    object's streaming response (single JSON or SSE) passes through untouched.
    """
    from hayate.adapters.workers import forward

    session_id = c.req.raw.headers.get(_SESSION_HEADER)
    stub = (
        binding.get(binding.idFromString(session_id))
        if session_id
        else binding.get(binding.newUniqueId())
    )
    return await forward(c, stub)
