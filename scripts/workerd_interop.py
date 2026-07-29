"""Official MCP SDK client check against the live workerd endpoint."""

from __future__ import annotations

import asyncio
import sys

from mcp import Client


async def check(endpoint: str) -> None:
    async with Client(endpoint) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "hayate-echo-workers"

        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["echo"]

        result = await client.call_tool("echo", {"text": "official-sdk-to-workerd"})
        assert result.content[0].text == "echo: official-sdk-to-workerd"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: workerd_interop.py ENDPOINT")
    asyncio.run(asyncio.wait_for(check(sys.argv[1]), timeout=30))
