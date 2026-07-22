"""Stateless mode: each request runs the SDK Server to completion, no session."""

from conftest import INITIALIZE, LIST_TOOLS, build_server, call_tool, rpc_request
from hayate_mcp import McpMount


def stateless_mount() -> McpMount:
    return McpMount(build_server(), stateless=True)


async def test_initialize_without_a_session():
    mount = stateless_mount()
    res = await mount.fetch(rpc_request(INITIALIZE))
    assert res.status == 200
    data = await res.json()
    assert data["result"]["serverInfo"]["name"] == "test-tools"


async def test_tool_call_needs_no_prior_session():
    mount = stateless_mount()
    # No initialize, no Mcp-Session-Id: a stateless node handles it directly.
    listed = await mount.fetch(rpc_request(LIST_TOOLS))
    assert listed.status == 200
    assert (await listed.json())["result"]["tools"][0]["name"] == "echo"

    called = await mount.fetch(rpc_request(call_tool("stateless")))
    assert (await called.json())["result"]["content"][0]["text"] == "echo: stateless"


async def test_many_independent_requests():
    mount = stateless_mount()
    for i in range(5):
        res = await mount.fetch(rpc_request(call_tool(f"n{i}", request_id=i + 10)))
        body = await res.json()
        assert body["id"] == i + 10
        assert body["result"]["content"][0]["text"] == f"echo: n{i}"


async def test_get_is_405_in_stateless():
    mount = stateless_mount()
    res = await mount.fetch(rpc_request("", method="GET"))
    assert res.status == 405
    assert res.headers.get("allow") == "POST"


async def test_delete_is_noop_200_in_stateless():
    mount = stateless_mount()
    res = await mount.fetch(rpc_request("", method="DELETE"))
    assert res.status == 200


async def test_notification_is_accepted():
    mount = stateless_mount()
    res = await mount.fetch(rpc_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert res.status == 202


async def test_origin_still_enforced_in_stateless():
    mount = stateless_mount()
    res = await mount.fetch(rpc_request(INITIALIZE, origin="https://evil.example"))
    assert res.status == 403
