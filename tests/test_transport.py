"""Streamable HTTP transport against the pure fetch core."""

import json

from hayate import Request

from conftest import INITIALIZE, LIST_TOOLS, build_server, call_tool, handshake, rpc_request
from hayate_mcp import McpMount, MemorySessionStore


async def test_initialize_starts_a_session(mount):
    res = await mount.fetch(rpc_request(INITIALIZE))
    assert res.status == 200
    assert res.headers.get("content-type") == "application/json"
    assert res.headers.get("mcp-session-id")
    data = await res.json()
    assert data["id"] == 1
    assert data["result"]["serverInfo"]["name"] == "test-tools"


async def test_full_tool_flow(mount):
    session_id = await handshake(mount)

    listed = await mount.fetch(rpc_request(LIST_TOOLS, session_id=session_id))
    assert listed.status == 200
    tools = (await listed.json())["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["echo"]

    called = await mount.fetch(rpc_request(call_tool("hayate"), session_id=session_id))
    assert called.status == 200
    content = (await called.json())["result"]["content"]
    assert content[0]["text"] == "echo: hayate"


async def test_valid_post_refreshes_idle_timestamp(mount):
    session_id = await handshake(mount)
    session = mount.store.peek(session_id)
    assert session is not None
    session.last_seen = -1.0

    listed = await mount.fetch(rpc_request(LIST_TOOLS, session_id=session_id))

    assert listed.status == 200
    assert session.last_seen > -1.0


async def test_valid_get_refreshes_idle_timestamp(mount):
    session_id = await handshake(mount)
    session = mount.store.peek(session_id)
    assert session is not None
    session.last_seen = -1.0

    stream = await mount.fetch(rpc_request("", method="GET", session_id=session_id))

    assert stream.status == 200
    assert session.last_seen > -1.0


async def test_store_peek_preserves_get_touch_compatibility(mount):
    session_id = await handshake(mount)
    session = mount.store.peek(session_id)
    assert session is not None
    session.last_seen = -1.0

    assert mount.store.peek(session_id) is session
    assert session.last_seen == -1.0
    assert mount.store.get(session_id) is session
    assert session.last_seen > -1.0


async def test_delete_does_not_refresh_before_removal():
    class InspectingStore(MemorySessionStore):
        removed_last_seen: float | None = None

        async def remove(self, session_id: str) -> bool:
            session = self.peek(session_id)
            if session is not None:
                self.removed_last_seen = session.last_seen
            return await super().remove(session_id)

    store = InspectingStore()
    mount = McpMount(build_server(), store=store)
    session_id = await handshake(mount)
    session = store.peek(session_id)
    assert session is not None
    session.last_seen = -1.0

    deleted = await mount.fetch(rpc_request("", method="DELETE", session_id=session_id))

    assert deleted.status == 200
    assert store.removed_last_seen == -1.0


async def test_two_sessions_are_independent(mount):
    first = await handshake(mount)
    second = await handshake(mount)
    assert first != second
    for session_id in (first, second):
        res = await mount.fetch(rpc_request(call_tool("x"), session_id=session_id))
        assert res.status == 200


async def test_missing_session_header_is_400(mount):
    res = await mount.fetch(rpc_request(LIST_TOOLS))
    assert res.status == 400


async def test_unknown_session_is_404(mount):
    res = await mount.fetch(rpc_request(LIST_TOOLS, session_id="deadbeef"))
    assert res.status == 404


async def test_delete_terminates_the_session(mount):
    session_id = await handshake(mount)
    deleted = await mount.fetch(rpc_request("", method="DELETE", session_id=session_id))
    assert deleted.status == 200
    gone = await mount.fetch(rpc_request(LIST_TOOLS, session_id=session_id))
    assert gone.status == 404


async def test_get_without_session_is_400(mount):
    res = await mount.fetch(rpc_request("", method="GET"))
    assert res.status == 400


async def test_cross_origin_is_403(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, origin="https://evil.example"))
    assert res.status == 403


async def test_same_origin_and_trusted_origin_pass(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, origin="http://localhost"))
    assert res.status == 200


async def test_reflected_host_does_not_bypass_origin_validation(mount):
    request = rpc_request(
        INITIALIZE,
        origin="http://evil.example",
        url="http://evil.example/mcp",
    )
    res = await mount.fetch(request)
    assert res.status == 403


async def test_explicit_trusted_browser_origin_passes():
    trusted = McpMount(build_server(), trusted_origins=["https://console.example"])
    res = await trusted.fetch(
        Request(
            "https://api.example/mcp",
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "origin": "https://console.example",
            },
            body=json.dumps(INITIALIZE),
        )
    )
    assert res.status == 200
    await trusted.store.close_all()


async def test_batch_bodies_are_rejected(mount):
    res = await mount.fetch(rpc_request([INITIALIZE, LIST_TOOLS]))
    assert res.status == 400


async def test_invalid_json_is_400(mount):
    res = await mount.fetch(rpc_request("{not json"))
    assert res.status == 400


async def test_non_standard_json_constants_are_rejected(mount):
    payload = (
        '{"jsonrpc":"2.0","id":3,"method":"tools/call",'
        '"params":{"name":"echo","arguments":{"text":NaN}}}'
    )
    res = await mount.fetch(rpc_request(payload))
    assert res.status == 400


async def test_post_rejects_non_utf8_json(mount):
    res = await mount.fetch(
        Request(
            "http://localhost/mcp",
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            body=b'{"jsonrpc":"2.0","method":"notifications/\xff"}',
        )
    )
    assert res.status == 400


async def test_post_requires_json_content_type(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"content-type": "text/plain"}))
    assert res.status == 415


async def test_post_requires_json_and_sse_accept_types(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"accept": "application/json"}))
    assert res.status == 406


async def test_post_does_not_accept_a_zero_quality_media_type(mount):
    res = await mount.fetch(
        rpc_request(
            INITIALIZE,
            headers={"accept": "application/json;q=0, text/event-stream"},
        )
    )
    assert res.status == 406

    invalid_quality = await mount.fetch(
        rpc_request(
            INITIALIZE,
            headers={"accept": "application/json;q=2, text/event-stream"},
        )
    )
    assert invalid_quality.status == 406


async def test_get_requires_sse_accept_type(mount):
    session_id = await handshake(mount)
    res = await mount.fetch(
        rpc_request(
            "",
            method="GET",
            session_id=session_id,
            headers={"accept": "application/json"},
        )
    )
    assert res.status == 406


async def test_unknown_path_is_404(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, path="/other"))
    assert res.status == 404
