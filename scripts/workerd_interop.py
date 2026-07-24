"""Official MCP SDK client check against the live workerd endpoint."""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(endpoint: str) -> None:
    async with (
        streamable_http_client(endpoint) as (read, write, get_session_id),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.protocolVersion == "2025-11-25"
        assert initialized.serverInfo.name == "hayate-echo-workers"
        assert get_session_id() is None

        tools = await session.list_tools()
        assert [tool.name for tool in tools.tools] == ["echo"]

        result = await session.call_tool("echo", {"text": "official-sdk-to-workerd"})
        assert result.content[0].text == "echo: official-sdk-to-workerd"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: workerd_interop.py ENDPOINT")
    asyncio.run(asyncio.wait_for(check(sys.argv[1]), timeout=30))
