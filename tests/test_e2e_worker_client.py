"""Official MCP SDK client against the Workers-native 2025-11-25 runtime."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
PORT = 8931


@pytest.fixture(scope="module")
def worker_endpoint():
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "entry:app", "--port", str(PORT)],
        cwd=ROOT / "examples" / "workers",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
        },
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
                if process.poll() is not None:
                    raise RuntimeError("Workers-native example exited early") from None
                time.sleep(0.2)
        else:
            raise RuntimeError("Workers-native example did not start listening")
        yield f"http://127.0.0.1:{PORT}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_official_client_round_trip_on_worker_runtime(worker_endpoint):
    async with (
        streamable_http_client(worker_endpoint) as (read, write, get_session_id),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.protocolVersion == "2025-11-25"
        assert initialized.serverInfo.name == "hayate-echo-workers"
        assert get_session_id() is None

        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == ["echo"]

        result = await session.call_tool("echo", {"text": "official client"})
        assert result.content[0].text == "echo: official client"
