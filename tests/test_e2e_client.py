"""End to end with the official SDK client over real HTTP.

This is the compatibility bar that matters: if `streamablehttp_client` +
`ClientSession` work, MCP Inspector and Claude Code speak the same protocol
path (they all implement the same Streamable HTTP spec).
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
PORT = 8930


@pytest.fixture(scope="module")
def endpoint():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(PORT)],
        cwd=ROOT / "examples" / "echo",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("uvicorn exited early") from None
                time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn did not start listening")
        yield f"http://127.0.0.1:{PORT}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_official_client_full_round_trip(endpoint):
    async with (
        streamable_http_client(endpoint) as (read, write, get_session_id),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()
        assert result.serverInfo.name == "hayate-echo"
        assert get_session_id()

        tools = await session.list_tools()
        assert [tool.name for tool in tools.tools] == ["echo"]

        outcome = await session.call_tool("echo", {"text": "over the wire"})
        assert outcome.content[0].text == "echo: over the wire"
