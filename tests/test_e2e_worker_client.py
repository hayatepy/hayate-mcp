"""Official MCP SDK client against the Workers-native dual-era runtime."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client

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
    async with Client(worker_endpoint) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "hayate-echo-workers"

        listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == ["echo"]

        result = await client.call_tool("echo", {"text": "official client"})
        assert result.content[0].text == "echo: official client"
