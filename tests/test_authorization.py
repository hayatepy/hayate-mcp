"""MCP Authorization (RFC 9728 Protected Resource Metadata + Bearer)."""

import pytest
from hayate import Hayate

from conftest import INITIALIZE, build_server, rpc_request
from hayate_mcp import Authorization, McpMount

RESOURCE = "https://mcp.example.com"
AS = "https://auth.example.com"
METADATA_PATH = "/.well-known/oauth-protected-resource"


async def _verify(token: str):
    return {"sub": "user-1", "scope": "mcp"} if token == "good-token" else None


def test_authorization_canonicalizes_secure_resource_and_issuer():
    authorization = Authorization(
        resource="HTTPS://MCP.EXAMPLE.COM/mcp",
        authorization_servers=["HTTPS://AUTH.EXAMPLE.COM/tenant/"],
        verify_token=_verify,
    )
    assert authorization.resource == "https://mcp.example.com/mcp"
    assert authorization.authorization_servers == ["https://auth.example.com/tenant/"]


@pytest.mark.parametrize(
    ("resource", "issuer"),
    [
        ("http://mcp.example.com/mcp", AS),
        (RESOURCE, "http://auth.example.com"),
        (RESOURCE, "https://user@auth.example.com"),
    ],
)
def test_authorization_rejects_insecure_or_credentialed_endpoints(resource, issuer):
    with pytest.raises(ValueError):
        Authorization(
            resource=resource,
            authorization_servers=[issuer],
            verify_token=_verify,
        )


def authorized_mount() -> McpMount:
    return McpMount(
        build_server(),
        stateless=True,
        authorization=Authorization(
            resource=RESOURCE,
            authorization_servers=[AS],
            verify_token=_verify,
            scopes_supported=["mcp"],
        ),
    )


async def test_metadata_document_is_rfc9728(mount_unused=None):
    mount = authorized_mount()
    res = await mount.fetch(rpc_request("", method="GET", path=METADATA_PATH))
    assert res.status == 200
    doc = await res.json()
    assert doc["resource"] == RESOURCE
    assert doc["authorization_servers"] == [AS]
    assert doc["bearer_methods_supported"] == ["header"]
    assert doc["scopes_supported"] == ["mcp"]


async def test_metadata_is_public_no_token_needed():
    mount = authorized_mount()
    res = await mount.fetch(rpc_request("", method="GET", path=METADATA_PATH))
    assert res.status == 200


async def test_metadata_only_accepts_get():
    mount = authorized_mount()
    res = await mount.fetch(rpc_request({}, method="POST", path=METADATA_PATH))
    assert res.status == 405
    assert res.headers.get("allow") == "GET"


async def test_request_without_token_is_401_with_www_authenticate():
    mount = authorized_mount()
    res = await mount.fetch(rpc_request(INITIALIZE))
    assert res.status == 401
    www = res.headers.get("www-authenticate")
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{RESOURCE}{METADATA_PATH}"' in www


async def test_invalid_token_is_401():
    mount = authorized_mount()
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"authorization": "Bearer wrong"}))
    assert res.status == 401
    assert 'error="invalid_token"' in res.headers.get("www-authenticate")


async def test_malformed_authorization_header_is_401():
    mount = authorized_mount()
    for header in (
        "good-token",
        "Basic good-token",
        "Bearer",
        "Bearer ",
        "Bearer good-token extra",
    ):
        res = await mount.fetch(rpc_request(INITIALIZE, headers={"authorization": header}))
        assert res.status == 401, header


async def test_valid_token_is_accepted():
    mount = authorized_mount()
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"authorization": "Bearer good-token"}))
    assert res.status == 200
    assert (await res.json())["result"]["serverInfo"]["name"] == "test-tools"


async def test_required_scope_is_enforced_and_advertised():
    mount = McpMount(
        build_server(),
        stateless=True,
        authorization=Authorization(
            resource=RESOURCE,
            authorization_servers=[AS],
            verify_token=_verify,
            scopes_supported=["mcp", "documents:read"],
            required_scopes=["documents:read"],
        ),
    )
    res = await mount.fetch(rpc_request(INITIALIZE, headers={"authorization": "Bearer good-token"}))
    assert res.status == 403
    challenge = res.headers.get("www-authenticate")
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="documents:read"' in challenge


async def test_tool_scope_is_enforced():
    mount = McpMount(
        build_server(),
        stateless=True,
        authorization=Authorization(
            resource=RESOURCE,
            authorization_servers=[AS],
            verify_token=_verify,
            scopes_supported=["mcp", "documents:read"],
        ),
        tool_scopes={"echo": ["documents:read"]},
    )
    res = await mount.fetch(
        rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "secret"}},
            },
            headers={"authorization": "Bearer good-token"},
        )
    )
    assert res.status == 403
    assert 'scope="documents:read"' in res.headers.get("www-authenticate")


async def test_register_exposes_the_metadata_route():
    mount = authorized_mount()
    app = Hayate()
    mount.register(app)
    res = await app.request(METADATA_PATH)
    assert res.status == 200
    assert (await res.json())["resource"] == RESOURCE

    # And the MCP endpoint still enforces the token.
    denied = await app.request("/mcp", method="POST", json=INITIALIZE)
    assert denied.status == 401
    ok = await app.request(
        "/mcp",
        method="POST",
        json=INITIALIZE,
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer good-token",
        },
    )
    assert ok.status == 200


async def test_no_authorization_config_means_open_access():
    mount = McpMount(build_server(), stateless=True)
    res = await mount.fetch(rpc_request(INITIALIZE))
    assert res.status == 200
    # No metadata route when unauthenticated.
    meta = await mount.fetch(rpc_request("", method="GET", path=METADATA_PATH))
    assert meta.status == 404


PATHED_RESOURCE = "https://app.example.com/mcp"
INSERTED_PATH = "/.well-known/oauth-protected-resource/mcp"


def pathed_mount() -> McpMount:
    return McpMount(
        build_server(),
        stateless=True,
        authorization=Authorization(
            resource=PATHED_RESOURCE,
            authorization_servers=[AS],
            verify_token=_verify,
        ),
    )


async def test_pathed_resource_uses_rfc9728_path_insertion():
    """RFC 9728 §3.1 puts the well-known segment between host and path.
    Until 0.5.x it was appended after the path while the mount served the
    root form, so the advertised URL pointed at a 404 (found by the
    hayate-auth AS spike on workerd)."""
    mount = pathed_mount()

    res = await mount.fetch(rpc_request("", method="GET", path=INSERTED_PATH))
    assert res.status == 200
    assert (await res.json())["resource"] == PATHED_RESOURCE

    denied = await mount.fetch(rpc_request(INITIALIZE))
    www = denied.headers.get("www-authenticate")
    assert f'resource_metadata="https://app.example.com{INSERTED_PATH}"' in www

    # Both pre-0.6 wrong shapes are gone.
    appended = await mount.fetch(
        rpc_request("", method="GET", path="/mcp/.well-known/oauth-protected-resource")
    )
    assert appended.status == 404
    root = await mount.fetch(rpc_request("", method="GET", path=METADATA_PATH))
    assert root.status == 404


async def test_register_exposes_the_inserted_metadata_route():
    mount = pathed_mount()
    app = Hayate()
    mount.register(app)
    res = await app.request(INSERTED_PATH)
    assert res.status == 200
    assert (await res.json())["resource"] == PATHED_RESOURCE
