"""The v0.2 GET SSE stream: server-initiated messages reach the client."""

import asyncio

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from conftest import handshake, rpc_request

NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "notifications/message",
    "params": {"level": "info", "data": "server says hi"},
}


async def test_stream_delivers_server_initiated_messages(mount):
    session_id = await handshake(mount)
    res = await mount.fetch(rpc_request("", method="GET", session_id=session_id))
    assert res.status == 200
    assert res.headers.get("content-type") == "text/event-stream"

    session = mount.store.get(session_id)
    await session._from_server_send.send(
        SessionMessage(message=JSONRPCMessage.model_validate(NOTIFICATION))
    )
    for _ in range(3):  # let the reader task move the message to the queue
        await asyncio.sleep(0)
    await session.close()

    payload = (await res.bytes()).decode()
    assert "notifications/message" in payload
    assert "server says hi" in payload


async def test_unknown_session_is_404(mount):
    res = await mount.fetch(rpc_request("", method="GET", session_id="nope"))
    assert res.status == 404


async def test_second_stream_is_409(mount):
    session_id = await handshake(mount)
    first = await mount.fetch(rpc_request("", method="GET", session_id=session_id))
    assert first.status == 200
    second = await mount.fetch(rpc_request("", method="GET", session_id=session_id))
    assert second.status == 409


async def test_slot_frees_after_stream_ends(mount):
    session_id = await handshake(mount)
    first = await mount.fetch(rpc_request("", method="GET", session_id=session_id))
    session = mount.store.get(session_id)
    await session.close()
    await first.bytes()  # drain to completion; the generator releases the slot
    assert session.claim_stream() is True
