"""Origin allow-listing shared by the ASGI and Workers transports."""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def origin_allowed(
    origin: str | None,
    request_origin: str,
    trusted_origins: Collection[str],
) -> bool:
    """Validate a browser Origin without trusting its reflected Host header."""

    if origin is None:
        return True
    if origin == "null":
        return False
    if origin in trusted_origins:
        return True
    return _loopback_origin(origin) and _loopback_origin(request_origin)


def _loopback_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and hostname in _LOOPBACK_HOSTS
