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
from mcp import Client

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
    async with Client(endpoint) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "hayate-echo"

        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["echo"]

        outcome = await client.call_tool("echo", {"text": "over the wire"})
        assert outcome.content[0].text == "echo: over the wire"
