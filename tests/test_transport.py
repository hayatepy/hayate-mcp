"""Streamable HTTP transport against the pure fetch core."""

from conftest import INITIALIZE, LIST_TOOLS, call_tool, handshake, rpc_request


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


async def test_get_is_405_until_v02(mount):
    res = await mount.fetch(rpc_request("", method="GET"))
    assert res.status == 405
    assert res.headers.get("allow") == "POST, DELETE"


async def test_cross_origin_is_403(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, origin="https://evil.example"))
    assert res.status == 403


async def test_same_origin_and_trusted_origin_pass(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, origin="http://localhost"))
    assert res.status == 200


async def test_batch_bodies_are_rejected(mount):
    res = await mount.fetch(rpc_request([INITIALIZE, LIST_TOOLS]))
    assert res.status == 400


async def test_invalid_json_is_400(mount):
    res = await mount.fetch(rpc_request("{not json"))
    assert res.status == 400


async def test_unknown_path_is_404(mount):
    res = await mount.fetch(rpc_request(INITIALIZE, path="/other"))
    assert res.status == 404
