"""Verified claims reach tools and bind stateful sessions to their owner."""

import mcp.types as types
from mcp.server.lowlevel import Server

from conftest import INITIALIZE, INITIALIZED, LIST_TOOLS, rpc_request
from hayate_mcp import Authorization, McpMount, get_principal

RESOURCE = "https://mcp.example.com/mcp"


async def verify(token: str):
    if token in {"alice", "bob"}:
        return {
            "sub": token,
            "client_id": "folio-client",
            "scope": "mcp documents:read",
        }
    return None


def principal_server() -> Server:
    server = Server("principal")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="whoami",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        principal = get_principal()
        return [
            types.TextContent(
                type="text",
                text=f"{principal['subject']}:{' '.join(principal['scopes'])}",
            )
        ]

    return server


def authorized_mount(*, stateless: bool) -> McpMount:
    return McpMount(
        principal_server(),
        stateless=stateless,
        authorization=Authorization(
            resource=RESOURCE,
            authorization_servers=["https://auth.example.com"],
            verify_token=verify,
            scopes_supported=["mcp", "documents:read"],
            required_scopes=["mcp"],
        ),
    )


async def test_stateless_tool_sees_verified_principal():
    mount = authorized_mount(stateless=True)
    res = await mount.fetch(
        rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
            headers={"authorization": "Bearer alice"},
        )
    )
    assert res.status == 200
    assert (await res.json())["result"]["content"][0]["text"] == ("alice:mcp documents:read")


async def test_stateful_session_is_bound_to_creating_principal():
    mount = authorized_mount(stateless=False)
    try:
        init = await mount.fetch(rpc_request(INITIALIZE, headers={"authorization": "Bearer alice"}))
        session_id = init.headers.get("mcp-session-id")
        assert session_id
        initialized = await mount.fetch(
            rpc_request(
                INITIALIZED,
                session_id=session_id,
                headers={"authorization": "Bearer alice"},
            )
        )
        assert initialized.status == 202

        session = mount.store.peek(session_id)
        assert session is not None
        session.last_seen = -1.0
        denied = await mount.fetch(
            rpc_request(
                LIST_TOOLS,
                session_id=session_id,
                headers={"authorization": "Bearer bob"},
            )
        )
        assert denied.status == 404
        assert session.last_seen == -1.0

        allowed = await mount.fetch(
            rpc_request(
                LIST_TOOLS,
                session_id=session_id,
                headers={"authorization": "Bearer alice"},
            )
        )
        assert allowed.status == 200
        assert session.last_seen > -1.0
    finally:
        await mount.store.close_all()
