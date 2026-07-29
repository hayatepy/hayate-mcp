"""MCP-Protocol-Version header handling (Streamable HTTP transport, 2025-11-25)."""

import asyncio
from copy import deepcopy

from mcp.types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    LATEST_MODERN_VERSION,
    LATEST_PROTOCOL_VERSION,
)

from conftest import INITIALIZE, INITIALIZED, LIST_TOOLS, build_server, handshake, rpc_request
from hayate_mcp import McpMount


def _stateless() -> McpMount:
    return McpMount(build_server(), stateless=True)


async def _handshake_version(mount: McpMount, proposed: str) -> tuple[str, str]:
    initialize = deepcopy(INITIALIZE)
    initialize["params"]["protocolVersion"] = proposed
    res = await mount.fetch(rpc_request(initialize))
    assert res.status == 200
    body = await res.json()
    session_id = res.headers.get("mcp-session-id")
    assert session_id
    negotiated = body["result"]["protocolVersion"]
    accepted = await mount.fetch(
        rpc_request(
            INITIALIZED,
            session_id=session_id,
            headers={"mcp-protocol-version": negotiated},
        )
    )
    assert accepted.status == 202
    return session_id, negotiated


def test_sdk_speaks_the_latest_stable_revision():
    assert LATEST_PROTOCOL_VERSION == "2026-07-28"
    assert LATEST_MODERN_VERSION == "2026-07-28"
    assert LATEST_HANDSHAKE_VERSION == "2025-11-25"


async def test_each_supported_version_is_bound_to_its_own_session(mount):
    for proposed in HANDSHAKE_PROTOCOL_VERSIONS:
        session_id, negotiated = await _handshake_version(mount, proposed)
        assert negotiated == proposed
        session = mount.store.get(session_id)
        assert session is not None
        assert session.protocol_version == negotiated
        res = await mount.fetch(
            rpc_request(
                LIST_TOOLS,
                session_id=session_id,
                headers={"mcp-protocol-version": negotiated},
            )
        )
        assert res.status == 200, proposed


async def test_supported_but_non_negotiated_version_is_400(mount):
    session_id, negotiated = await _handshake_version(mount, LATEST_HANDSHAKE_VERSION)
    other = next(version for version in HANDSHAKE_PROTOCOL_VERSIONS if version != negotiated)
    session = mount.store.peek(session_id)
    assert session is not None
    session.last_seen = -1.0
    res = await mount.fetch(
        rpc_request(
            LIST_TOOLS,
            session_id=session_id,
            headers={"mcp-protocol-version": other},
        )
    )
    assert res.status == 400
    assert session.last_seen == -1.0


async def test_two_sessions_can_retain_different_negotiated_versions(mount):
    latest_id, latest = await _handshake_version(mount, LATEST_HANDSHAKE_VERSION)
    older_id, older = await _handshake_version(mount, "2025-06-18")
    assert latest != older

    for session_id, version in ((latest_id, latest), (older_id, older)):
        matching = await mount.fetch(
            rpc_request(
                LIST_TOOLS,
                session_id=session_id,
                headers={"mcp-protocol-version": version},
            )
        )
        assert matching.status == 200

    crossed = await mount.fetch(
        rpc_request(
            LIST_TOOLS,
            session_id=latest_id,
            headers={"mcp-protocol-version": older},
        )
    )
    assert crossed.status == 400


async def test_concurrent_posts_use_the_sessions_negotiated_version(mount):
    session_id, negotiated = await _handshake_version(mount, LATEST_HANDSHAKE_VERSION)

    requests = []
    for request_id in range(1000, 1003):
        message = deepcopy(LIST_TOOLS)
        message["id"] = request_id
        requests.append(
            mount.fetch(
                rpc_request(
                    message,
                    session_id=session_id,
                    headers={"mcp-protocol-version": negotiated},
                )
            )
        )

    responses = await asyncio.gather(*requests)
    assert [response.status for response in responses] == [200, 200, 200]
    assert [(await response.json())["id"] for response in responses] == [1000, 1001, 1002]


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


async def test_unknown_version_header_is_routed_to_modern_validation(mount):
    # SDK v2 routes every non-handshake version through the modern ladder.
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"mcp-protocol-version": "whatever"}))
    assert res.status == 400
    assert (await res.json())["error"]["code"] == -32602


async def test_get_with_supported_but_non_negotiated_version_is_400(mount):
    session_id, negotiated = await _handshake_version(mount, LATEST_HANDSHAKE_VERSION)
    other = next(version for version in HANDSHAKE_PROTOCOL_VERSIONS if version != negotiated)
    res = await mount.fetch(
        rpc_request(
            "",
            method="GET",
            session_id=session_id,
            headers={"mcp-protocol-version": other},
        )
    )
    assert res.status == 400


async def test_delete_requires_the_negotiated_version(mount):
    session_id, negotiated = await _handshake_version(mount, LATEST_HANDSHAKE_VERSION)
    other = next(version for version in HANDSHAKE_PROTOCOL_VERSIONS if version != negotiated)

    mismatch = await mount.fetch(
        rpc_request(
            "",
            method="DELETE",
            session_id=session_id,
            headers={"mcp-protocol-version": other},
        )
    )
    assert mismatch.status == 400

    matching = await mount.fetch(
        rpc_request(
            "",
            method="DELETE",
            session_id=session_id,
            headers={"mcp-protocol-version": negotiated},
        )
    )
    assert matching.status == 200
    assert mount.store.get(session_id) is None


async def test_failed_initialize_does_not_leave_a_session():
    mount = McpMount(build_server(), session_id="failed-initialize")
    invalid = deepcopy(INITIALIZE)
    invalid["params"]["clientInfo"] = {}
    try:
        res = await mount.fetch(rpc_request(invalid))
        body = await res.json()
        assert "error" in body
        assert res.headers.get("mcp-session-id") is None
        assert mount.store.get("failed-initialize") is None
    finally:
        await mount.store.close_all()


async def test_stateless_validates_version_on_non_initialize():
    mount = _stateless()
    ok = await mount.fetch(
        rpc_request(LIST_TOOLS, headers={"mcp-protocol-version": LATEST_HANDSHAKE_VERSION})
    )
    assert ok.status == 200
    bad = await mount.fetch(rpc_request(LIST_TOOLS, headers={"mcp-protocol-version": "2000-01-01"}))
    assert bad.status == 400
