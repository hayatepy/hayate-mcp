"""MCP-Protocol-Version header handling (Streamable HTTP transport, 2025-11-25)."""

from mcp.shared.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

from conftest import INITIALIZE, LIST_TOOLS, build_server, handshake, rpc_request
from hayate_mcp import McpMount


def _stateless() -> McpMount:
    return McpMount(build_server(), stateless=True)


def test_sdk_speaks_the_latest_stable_revision():
    assert LATEST_PROTOCOL_VERSION == "2025-11-25"
    assert "2025-06-18" in SUPPORTED_PROTOCOL_VERSIONS


async def test_supported_version_header_passes(mount):
    session_id = await handshake(mount)
    for version in SUPPORTED_PROTOCOL_VERSIONS:
        res = await mount.fetch(
            rpc_request(
                LIST_TOOLS, session_id=session_id, headers={"mcp-protocol-version": version}
            )
        )
        assert res.status == 200, version


async def test_unsupported_version_header_is_400(mount):
    session_id = await handshake(mount)
    res = await mount.fetch(
        rpc_request(
            LIST_TOOLS, session_id=session_id, headers={"mcp-protocol-version": "1999-01-01"}
        )
    )
    assert res.status == 400


async def test_missing_version_header_passes_for_backcompat(mount):
    session_id = await handshake(mount)
    res = await mount.fetch(rpc_request(LIST_TOOLS, session_id=session_id))
    assert res.status == 200


async def test_initialize_is_exempt_from_version_header(mount):
    # initialize carries no negotiated version yet, so an odd header is fine.
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"mcp-protocol-version": "whatever"}))
    assert res.status == 200


async def test_get_with_unsupported_version_is_400(mount):
    session_id = await handshake(mount)
    res = await mount.fetch(
        rpc_request(
            "", method="GET", session_id=session_id, headers={"mcp-protocol-version": "nope"}
        )
    )
    assert res.status == 400


async def test_stateless_validates_version_on_non_initialize():
    mount = _stateless()
    ok = await mount.fetch(
        rpc_request(LIST_TOOLS, headers={"mcp-protocol-version": LATEST_PROTOCOL_VERSION})
    )
    assert ok.status == 200
    bad = await mount.fetch(rpc_request(LIST_TOOLS, headers={"mcp-protocol-version": "2000-01-01"}))
    assert bad.status == 400
