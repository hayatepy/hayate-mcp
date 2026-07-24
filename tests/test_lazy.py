"""Workers-safe lazy mount creation and registration."""

import subprocess
import sys

from hayate import Hayate

from conftest import INITIALIZE, build_server
from hayate_mcp import LazyMcpMount


def test_importing_lazy_mount_does_not_import_mcp_sdk():
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from hayate_mcp import LazyMcpMount; "
                "assert 'mcp' not in sys.modules; "
                "assert LazyMcpMount"
            ),
        ],
        check=True,
    )


async def test_lazy_mount_builds_once_and_registers_all_transport_routes():
    calls = 0

    def factory(c):
        nonlocal calls
        calls += 1
        from hayate_mcp import McpMount

        return McpMount(build_server(), stateless=True)

    lazy = LazyMcpMount(factory)
    app = Hayate()
    lazy.register(app)

    for _ in range(2):
        res = await app.request(
            "/mcp",
            method="POST",
            json=INITIALIZE,
            headers={"accept": "application/json, text/event-stream"},
        )
        assert res.status == 200
    assert calls == 1
