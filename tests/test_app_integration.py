"""McpMount.register on a real Hayate app, driven via app.request."""

import json

from hayate import Hayate

from conftest import INITIALIZE, LIST_TOOLS, build_server, handshake, rpc_request
from hayate_mcp import McpMount


async def test_mounted_flow_through_the_app():
    mount = McpMount(build_server(), path="/mcp")
    app = Hayate()
    mount.register(app)

    @app.get("/")
    async def home(c):
        return c.json({"ok": True})

    try:
        init = await app.request(
            "/mcp",
            method="POST",
            json=INITIALIZE,
            headers={"accept": "application/json"},
        )
        assert init.status == 200
        session_id = init.headers.get("mcp-session-id")

        listed = await app.request(
            "/mcp",
            method="POST",
            json=LIST_TOOLS,
            headers={"mcp-session-id": session_id},
        )
        assert listed.status == 200
        assert (await listed.json())["result"]["tools"][0]["name"] == "echo"

        # The rest of the app is untouched.
        assert (await app.request("/")).status == 200
    finally:
        await mount.store.close_all()


async def test_custom_path():
    mount = McpMount(build_server(), path="/api/mcp")
    try:
        res = await mount.fetch(rpc_request(INITIALIZE, path="/api/mcp"))
        assert res.status == 200
    finally:
        await mount.store.close_all()


async def test_store_eviction_closes_idle_sessions():
    mount = McpMount(build_server())
    mount.store.idle_ttl = 0.0  # everything is instantly idle
    try:
        first = await handshake(mount)
        await handshake(mount)  # triggers eviction of the first
        gone = await mount.fetch(rpc_request(json.loads(json.dumps(LIST_TOOLS)), session_id=first))
        assert gone.status == 404
    finally:
        await mount.store.close_all()
