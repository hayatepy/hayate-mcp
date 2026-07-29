"""MCP protocol-era and HTTP routing primitives shared by both runtimes.

This module deliberately has no dependency on the official MCP SDK.  It can
therefore be imported by Cloudflare Python Workers while keeping the CPython
and Workers transports on the same 2026-07-28 validation ladder.

The header codec and ``x-mcp-header`` rules follow the MCP Python SDK 2.0
implementation, which is the executable reference used by the official
conformance suite.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

LEGACY_PROTOCOL_VERSION: Final = "2025-11-25"
MODERN_PROTOCOL_VERSION: Final = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    LEGACY_PROTOCOL_VERSION,
)
MODERN_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = (MODERN_PROTOCOL_VERSION,)

PROTOCOL_VERSION_META_KEY: Final = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY: Final = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY: Final = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY: Final = "io.modelcontextprotocol/serverInfo"

MCP_PROTOCOL_VERSION_HEADER: Final = "mcp-protocol-version"
MCP_METHOD_HEADER: Final = "mcp-method"
MCP_NAME_HEADER: Final = "mcp-name"
MCP_PARAM_HEADER_PREFIX: Final = "Mcp-Param-"
X_MCP_HEADER_KEY: Final = "x-mcp-header"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
HEADER_MISMATCH: Final = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY: Final = -32021
UNSUPPORTED_PROTOCOL_VERSION: Final = -32022

ERROR_CODE_HTTP_STATUS: Final[Mapping[int, int]] = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    MISSING_REQUIRED_CLIENT_CAPABILITY: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
    METHOD_NOT_FOUND: 404,
}

NAME_BEARING_METHODS: Final[Mapping[str, str]] = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}
MODERN_REMOVED_METHODS: Final = frozenset(
    {
        "initialize",
        "ping",
        "logging/setLevel",
        "resources/subscribe",
        "resources/unsubscribe",
    }
)

_B64_SENTINEL = re.compile(r"^=\?base64\?(?P<payload>.*)\?=$")
_HEADER_SAFE = re.compile(r"^[\x20-\x7E]*$")
_RFC9110_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CANONICAL_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_X_MCP_HEADER_PRIMITIVE_TYPES: Final = frozenset({"string", "integer", "boolean"})
_JS_SAFE_INTEGER_MAX: Final = 2**53 - 1
_ROUTING_HEADER_NAMES: Final = frozenset(
    {
        MCP_PROTOCOL_VERSION_HEADER,
        MCP_METHOD_HEADER,
        MCP_NAME_HEADER,
    }
)
_SUBSCHEMA_SINGLE: Final = frozenset(
    {
        "items",
        "contains",
        "unevaluatedItems",
        "additionalProperties",
        "propertyNames",
        "unevaluatedProperties",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_SUBSCHEMA_LIST: Final = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SUBSCHEMA_MAP: Final = frozenset({"patternProperties", "dependentSchemas", "$defs", "definitions"})


@dataclass(frozen=True, slots=True)
class ModernRequest:
    """A request that passed the 2026-07-28 metadata and HTTP header ladder."""

    protocol_version: str
    client_capabilities: Any
    client_info: Any


@dataclass(frozen=True, slots=True)
class ProtocolRejection:
    """A structured JSON-RPC rejection produced before method dispatch."""

    code: int
    message: str
    data: Any | None = None

    @property
    def http_status(self) -> int:
        return ERROR_CODE_HTTP_STATUS.get(self.code, 200)


def lower_headers(headers: Any) -> dict[str, str]:
    """Return a case-insensitive transport view suitable for MCP validation."""

    return {str(name).lower(): str(value) for name, value in headers.items()}


def find_duplicated_routing_header(
    headers: Iterable[tuple[str, str]],
) -> str | None:
    """Return the first duplicated standard routing header, if any."""

    seen: set[str] = set()
    for name, _value in headers:
        folded = name.lower()
        if folded in _ROUTING_HEADER_NAMES:
            if folded in seen:
                return folded
            seen.add(folded)
    return None


def has_modern_envelope(body: Any) -> bool:
    """Whether a decoded request claims the per-request modern protocol era."""

    if not isinstance(body, Mapping):
        return False
    params = body.get("params")
    if not isinstance(params, Mapping):
        return False
    meta = params.get("_meta")
    return isinstance(meta, Mapping) and PROTOCOL_VERSION_META_KEY in meta


def should_route_modern(body: Any, headers: Mapping[str, str]) -> bool:
    """Apply the dual-era HTTP routing rule.

    A non-handshake protocol header is routed to the modern validation ladder,
    including unknown versions so the client receives ``-32022``.  Inspecting
    the envelope as a fallback gives a missing HTTP header the required
    ``HeaderMismatch`` response instead of accidentally treating it as legacy.
    """

    version = headers.get(MCP_PROTOCOL_VERSION_HEADER)
    return (version is not None and version not in LEGACY_PROTOCOL_VERSIONS) or has_modern_envelope(
        body
    )


def classify_modern_request(
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    supported_versions: Sequence[str] = MODERN_PROTOCOL_VERSIONS,
) -> ModernRequest | ProtocolRejection:
    """Validate a modern request's mandatory envelope and routing headers."""

    params = body.get("params")
    meta_value = params.get("_meta") if isinstance(params, Mapping) else None
    if not isinstance(meta_value, Mapping):
        return ProtocolRejection(
            INVALID_PARAMS,
            "params._meta must be an object carrying the required "
            f"{PROTOCOL_VERSION_META_KEY!r} and "
            f"{CLIENT_CAPABILITIES_META_KEY!r} envelope keys",
        )
    meta = cast("Mapping[str, Any]", meta_value)
    missing = [
        key for key in (PROTOCOL_VERSION_META_KEY, CLIENT_CAPABILITIES_META_KEY) if key not in meta
    ]
    if missing:
        return ProtocolRejection(
            INVALID_PARAMS,
            f"params._meta is missing the required envelope key(s): {', '.join(missing)}",
        )

    protocol_version = meta[PROTOCOL_VERSION_META_KEY]
    method = body.get("method")
    version_header = headers.get(MCP_PROTOCOL_VERSION_HEADER)
    if not _plain_header_value_valid(version_header) or version_header != protocol_version:
        return ProtocolRejection(
            HEADER_MISMATCH,
            f"{MCP_PROTOCOL_VERSION_HEADER} header does not match "
            "the request envelope's protocol version",
        )
    method_header = headers.get(MCP_METHOD_HEADER)
    if not _plain_header_value_valid(method_header) or method_header != method:
        return ProtocolRejection(
            HEADER_MISMATCH,
            f"{MCP_METHOD_HEADER} header does not match the request body's method",
        )

    name_key = NAME_BEARING_METHODS.get(method) if isinstance(method, str) else None
    if name_key is not None:
        body_value = params.get(name_key) if isinstance(params, Mapping) else None
        if (
            body_value is not None
            and decode_header_value(headers.get(MCP_NAME_HEADER)) != body_value
        ):
            return ProtocolRejection(
                HEADER_MISMATCH,
                f"{MCP_NAME_HEADER} header does not match "
                f"the request body's {name_key!r} parameter",
            )

    if not isinstance(protocol_version, str):
        return ProtocolRejection(
            INVALID_PARAMS,
            "the protocol-version envelope value must be a string",
        )
    if protocol_version not in supported_versions:
        return ProtocolRejection(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {
                "supported": list(supported_versions),
                "requested": protocol_version,
            },
        )
    client_capabilities = meta[CLIENT_CAPABILITIES_META_KEY]
    if not isinstance(client_capabilities, Mapping):
        return ProtocolRejection(
            INVALID_PARAMS,
            "the client-capabilities envelope value must be an object",
        )
    client_info = meta.get(CLIENT_INFO_META_KEY)
    if client_info is not None and (
        not isinstance(client_info, Mapping)
        or not isinstance(client_info.get("name"), str)
        or not client_info["name"]
        or not isinstance(client_info.get("version"), str)
        or not client_info["version"]
    ):
        return ProtocolRejection(
            INVALID_PARAMS,
            "the optional client-info envelope value must identify a name and version",
        )
    return ModernRequest(
        protocol_version=protocol_version,
        client_capabilities=client_capabilities,
        client_info=client_info,
    )


def require_client_capabilities(
    declared: Any,
    required: Mapping[str, Any],
) -> ProtocolRejection | None:
    """Reject use of a feature whose client capabilities were not declared.

    Capability values are structural objects.  A required empty object means
    that the capability key itself must be present; nested requirements (for
    example an extension identifier or ``sampling.tools``) are checked
    recursively.
    """

    if _capabilities_contain(declared, required):
        return None
    return ProtocolRejection(
        MISSING_REQUIRED_CLIENT_CAPABILITY,
        "Client did not declare the capability required by this request",
        {"requiredCapabilities": _copy_json_object(required)},
    )


def _capabilities_contain(declared: Any, required: Mapping[str, Any]) -> bool:
    if not isinstance(declared, Mapping):
        return False
    for key, expected in required.items():
        if key not in declared:
            return False
        actual = declared[key]
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping):
                return False
            if expected and not _capabilities_contain(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def _copy_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _copy_json_object(item) if isinstance(item, Mapping) else item
        for key, item in value.items()
    }


def encode_header_value(value: str) -> str:
    """Encode a routed value without losing Unicode or edge whitespace."""

    if (
        _HEADER_SAFE.fullmatch(value)
        and value == value.strip()
        and not _B64_SENTINEL.fullmatch(value)
    ):
        return value
    payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{payload}?="


def decode_header_value(value: str | None) -> str | None:
    """Decode the MCP base64 sentinel, rejecting malformed encodings."""

    if value is None:
        return None
    match = _B64_SENTINEL.fullmatch(value)
    if match is None:
        return value if _plain_header_value_valid(value) else None
    payload = match.group("payload")
    try:
        decoded = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None
    if base64.b64encode(decoded).decode("ascii") != payload:
        return None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_invalid_x_mcp_header(input_schema: Any) -> str | None:
    """Return the first invalid ``x-mcp-header`` annotation, if any."""

    seen: dict[str, str] = {}
    for path, schema in _walk_schema_positions(input_schema):
        if X_MCP_HEADER_KEY not in schema:
            continue
        if not path:
            return (
                f"{X_MCP_HEADER_KEY} found at a schema position not reachable "
                "via a pure `properties` chain"
            )
        where = ".".join(path)
        header = schema[X_MCP_HEADER_KEY]
        if not isinstance(header, str):
            return (
                f"property {where!r}: {X_MCP_HEADER_KEY} must be a string, "
                f"not {type(header).__name__}"
            )
        if not _RFC9110_TOKEN.fullmatch(header):
            return f"property {where!r}: {X_MCP_HEADER_KEY} {header!r} is not an RFC 9110 token"
        prop_type = schema.get("type")
        if prop_type not in _X_MCP_HEADER_PRIMITIVE_TYPES:
            return (
                f"property {where!r}: {X_MCP_HEADER_KEY} is only permitted on "
                f"integer/string/boolean properties (got {prop_type!r})"
            )
        folded = header.lower()
        if folded in seen:
            return (
                f"{X_MCP_HEADER_KEY} {header!r} on property {where!r} "
                f"duplicates property {seen[folded]!r}"
            )
        seen[folded] = where
    return None


def validate_mcp_param_headers(
    input_schema: Any,
    arguments: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    raw_headers: Iterable[tuple[str, str]] | None = None,
) -> ProtocolRejection | None:
    """Cross-check every declared ``Mcp-Param-*`` header with tool arguments."""

    if find_invalid_x_mcp_header(input_schema) is not None:
        return None
    folded = lower_headers(headers)
    duplicated: set[str] = set()
    seen: set[str] = set()
    for name, _value in raw_headers if raw_headers is not None else headers.items():
        key = name.lower()
        if key in seen:
            duplicated.add(key)
        seen.add(key)
    for path, token, schema in _annotated_positions(input_schema):
        header_name = f"{MCP_PARAM_HEADER_PREFIX}{token}"
        key = header_name.lower()
        raw = folded.get(key)
        value = _value_at_path(arguments, path)
        argument = ".".join(path)
        if raw is not None and key in duplicated:
            return ProtocolRejection(
                HEADER_MISMATCH,
                f"{header_name} header appears more than once",
            )
        if value is None:
            if raw is not None:
                return ProtocolRejection(
                    HEADER_MISMATCH,
                    f"{header_name} header is present but the request body's "
                    f"{argument!r} argument is absent",
                )
            continue
        rendered = _render_header_scalar(value)
        if rendered is None:
            if raw is not None:
                return ProtocolRejection(
                    HEADER_MISMATCH,
                    f"{header_name} header does not match the request body's {argument!r} argument",
                )
            continue
        if raw is None:
            return ProtocolRejection(
                HEADER_MISMATCH,
                f"{header_name} header is missing but the request body's "
                f"{argument!r} argument is present",
            )
        decoded = decode_header_value(raw)
        if decoded is None:
            return ProtocolRejection(
                HEADER_MISMATCH,
                f"{header_name} header carries a malformed base64 sentinel value",
            )
        if not _mcp_param_value_matches(schema.get("type"), value, rendered, decoded):
            return ProtocolRejection(
                HEADER_MISMATCH,
                f"{header_name} header does not match the request body's {argument!r} argument",
            )
    return None


def _walk_schema_positions(
    root: Any,
) -> Iterator[tuple[tuple[str, ...] | None, dict[str, Any]]]:
    stack: list[tuple[tuple[str, ...] | None, Any]] = [((), root)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue
        schema = cast("dict[str, Any]", node)
        yield path, schema
        for keyword, value in schema.items():
            if keyword == "properties" and isinstance(value, dict):
                for name, child in cast("dict[str, Any]", value).items():
                    stack.append(((*path, name) if path is not None else None, child))
            elif keyword in _SUBSCHEMA_SINGLE:
                stack.append((None, value))
            elif keyword in _SUBSCHEMA_LIST and isinstance(value, list):
                stack.extend((None, child) for child in value)
            elif keyword in _SUBSCHEMA_MAP and isinstance(value, dict):
                stack.extend((None, child) for child in cast("dict[str, Any]", value).values())


def _annotated_positions(
    input_schema: Any,
) -> Iterator[tuple[tuple[str, ...], str, dict[str, Any]]]:
    for path, schema in _walk_schema_positions(input_schema):
        token = schema.get(X_MCP_HEADER_KEY)
        if path and isinstance(token, str):
            yield path, token, schema


def _value_at_path(arguments: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = arguments
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = cast("Mapping[str, Any]", node).get(key)
    return node


def _render_header_scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str | int | float):
        return None
    try:
        return str(value)
    except ValueError:
        return None


def _mcp_param_value_matches(
    property_type: Any,
    value: Any,
    rendered: str,
    decoded: str,
) -> bool:
    integral_value = isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if (
        property_type == "integer"
        and not isinstance(value, bool)
        and integral_value
        and abs(int(value)) > _JS_SAFE_INTEGER_MAX
    ):
        return False
    if (
        property_type == "integer"
        and not isinstance(value, bool)
        and (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
        and _CANONICAL_DECIMAL.fullmatch(decoded) is not None
    ):
        whole, _, fraction = decoded.partition(".")
        if fraction and set(fraction) != {"0"}:
            return False
        try:
            return int(whole) == int(value)
        except ValueError:
            return False
    return decoded == rendered


def _plain_header_value_valid(value: str | None) -> bool:
    return (
        value is not None and _HEADER_SAFE.fullmatch(value) is not None and value == value.strip()
    )
