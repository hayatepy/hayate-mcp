"""Official MCP conformance fixture mounted through hayate-mcp.

Names intentionally match ``modelcontextprotocol/conformance``. This is test
infrastructure, not a recommended application surface.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as types
from hayate import Hayate
from mcp.server.caching import CacheHint
from mcp.server.lowlevel import Server
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ToolsListChanged,
)

from hayate_mcp import McpMount

TEST_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0g"
    "AAAABJRU5ErkJggg=="
)
TEST_AUDIO_BASE64 = "UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAAB9AAACABAAZGF0YQIAAAA="
JSON_SCHEMA_2020_12 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "$defs": {
        "address": {
            "$anchor": "addressDef",
            "type": "object",
            "properties": {
                "street": {"type": "string"},
                "city": {"type": "string"},
            },
        }
    },
    "properties": {
        "name": {"type": "string"},
        "address": {"$ref": "#/$defs/address"},
        "contactMethod": {"type": "string", "enum": ["phone", "email"]},
        "phone": {"type": "string"},
        "email": {"type": "string"},
    },
    "allOf": [{"anyOf": [{"required": ["phone"]}, {"required": ["email"]}]}],
    "if": {
        "properties": {"contactMethod": {"const": "phone"}},
        "required": ["contactMethod"],
    },
    "then": {"required": ["phone"]},
    "else": {"required": ["email"]},
    "additionalProperties": False,
}
EMPTY_SCHEMA = {"type": "object", "properties": {}}
MRTR_TOOL_NAMES = (
    "test_input_required_result_elicitation",
    "test_input_required_result_sampling",
    "test_input_required_result_list_roots",
    "test_input_required_result_request_state",
    "test_input_required_result_multiple_inputs",
    "test_input_required_result_multi_round",
    "test_input_required_result_tampered_state",
    "test_input_required_result_capabilities",
)
subscription_bus = InMemorySubscriptionBus()


async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
    definitions: list[tuple[str, str, dict[str, Any]]] = [
        ("test_simple_text", "Tests simple text content.", EMPTY_SCHEMA),
        ("test_image_content", "Tests image content.", EMPTY_SCHEMA),
        ("test_audio_content", "Tests audio content.", EMPTY_SCHEMA),
        ("test_embedded_resource", "Tests embedded resource content.", EMPTY_SCHEMA),
        ("test_multiple_content_types", "Tests mixed content.", EMPTY_SCHEMA),
        ("test_tool_with_logging", "Tests log notifications.", EMPTY_SCHEMA),
        ("test_error_handling", "Tests model-visible tool errors.", EMPTY_SCHEMA),
        ("test_tool_with_progress", "Tests progress notifications.", EMPTY_SCHEMA),
        (
            "test_sampling",
            "Tests a server-initiated sampling request.",
            {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        ),
        (
            "test_elicitation",
            "Tests a server-initiated elicitation request.",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        ),
        (
            "test_elicitation_sep1034_defaults",
            "Tests elicitation defaults.",
            EMPTY_SCHEMA,
        ),
        (
            "test_elicitation_sep1330_enums",
            "Tests elicitation enums.",
            EMPTY_SCHEMA,
        ),
        (
            "json_schema_2020_12_tool",
            "Tool with JSON Schema 2020-12 features.",
            JSON_SCHEMA_2020_12,
        ),
        (
            "test_header_echo",
            "Validates SEP-2243 custom request parameter headers.",
            {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "x-mcp-header": "Text",
                    }
                },
            },
        ),
        (
            "test_missing_capability",
            "Requires the client sampling capability.",
            EMPTY_SCHEMA,
        ),
        (
            "test_trigger_tool_change",
            "Publishes a tools-list change for subscription conformance.",
            EMPTY_SCHEMA,
        ),
        (
            "test_trigger_prompt_change",
            "Publishes a prompts-list change for subscription conformance.",
            EMPTY_SCHEMA,
        ),
    ]
    definitions.extend(
        (name, "Exercises SEP-2322 multi-round-trip input.", EMPTY_SCHEMA)
        for name in MRTR_TOOL_NAMES
    )
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=description, input_schema=schema)
            for name, description, schema in definitions
        ]
    )


def _tool_result(
    content: list[types.ContentBlock],
    *,
    is_error: bool = False,
) -> types.CallToolResult:
    return types.CallToolResult(content=content, is_error=is_error)


def _elicitation_input(
    message: str,
    properties: dict[str, Any],
    required: list[str],
) -> types.ElicitRequest:
    return types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=message,
            requested_schema={
                "type": "object",
                "properties": properties,
                "required": required,
            },
        )
    )


def _sampling_input(message: str, *, max_tokens: int = 100) -> types.CreateMessageRequest:
    return types.CreateMessageRequest(
        params=types.CreateMessageRequestParams(
            messages=[
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=message),
                )
            ],
            max_tokens=max_tokens,
        )
    )


def _roots_input() -> types.ListRootsRequest:
    return types.ListRootsRequest(params=types.RequestParams())


def _input_required(
    input_requests: dict[
        str,
        types.CreateMessageRequest | types.ListRootsRequest | types.ElicitRequest,
    ],
    *,
    request_state: str | None = None,
) -> types.InputRequiredResult:
    return types.InputRequiredResult(
        input_requests=input_requests,
        request_state=request_state,
    )


def _elicited_content(
    responses: dict[str, Any] | None,
    key: str,
) -> dict[str, Any] | None:
    response = (responses or {}).get(key)
    if not isinstance(response, types.ElicitResult) or response.action != "accept":
        return None
    return response.content


async def call_tool(
    ctx: Any,
    params: types.CallToolRequestParams,
) -> types.CallToolResult | types.InputRequiredResult:
    name = params.name
    arguments = params.arguments or {}
    input_responses = params.input_responses
    if name == "test_input_required_result_elicitation":
        content = _elicited_content(input_responses, "user_name")
        if content is None or not isinstance(content.get("name"), str):
            return _input_required(
                {
                    "user_name": _elicitation_input(
                        "What is your name?",
                        {"name": {"type": "string"}},
                        ["name"],
                    )
                }
            )
        return _tool_result([types.TextContent(type="text", text=f"Hello, {content['name']}!")])
    if name == "test_input_required_result_sampling":
        response = (input_responses or {}).get("capital_question")
        if not isinstance(response, types.CreateMessageResult):
            return _input_required(
                {"capital_question": _sampling_input("What is the capital of France?")}
            )
        text = getattr(response.content, "text", "")
        return _tool_result([types.TextContent(type="text", text=f"Sampling response: {text}")])
    if name == "test_input_required_result_list_roots":
        response = (input_responses or {}).get("client_roots")
        if not isinstance(response, types.ListRootsResult):
            return _input_required({"client_roots": _roots_input()})
        roots = ", ".join(str(root.uri) for root in response.roots)
        return _tool_result([types.TextContent(type="text", text=f"Client roots: {roots}")])
    if name in (
        "test_input_required_result_request_state",
        "test_input_required_result_tampered_state",
    ):
        content = _elicited_content(input_responses, "confirm")
        if content is None:
            return _input_required(
                {
                    "confirm": _elicitation_input(
                        "Please confirm",
                        {"ok": {"type": "boolean"}},
                        ["ok"],
                    )
                },
                request_state="request-state-v1",
            )
        if params.request_state != "request-state-v1":
            from mcp.shared.exceptions import MCPError

            raise MCPError(code=-32602, message="Invalid or expired requestState")
        return _tool_result([types.TextContent(type="text", text="state-ok: confirmed")])
    if name == "test_input_required_result_multiple_inputs":
        responses = input_responses or {}
        if not all(key in responses for key in ("user_name", "greeting", "client_roots")):
            return _input_required(
                {
                    "user_name": _elicitation_input(
                        "What is your name?",
                        {"name": {"type": "string"}},
                        ["name"],
                    ),
                    "greeting": _sampling_input(
                        "Generate a greeting",
                        max_tokens=50,
                    ),
                    "client_roots": _roots_input(),
                },
                request_state="multiple-inputs-v1",
            )
        return _tool_result([types.TextContent(type="text", text="All requested inputs received")])
    if name == "test_input_required_result_multi_round":
        if params.request_state is None:
            return _input_required(
                {
                    "step1": _elicitation_input(
                        "Step 1: What is your name?",
                        {"name": {"type": "string"}},
                        ["name"],
                    )
                },
                request_state="multi-round-1",
            )
        if params.request_state == "multi-round-1":
            return _input_required(
                {
                    "step2": _elicitation_input(
                        "Step 2: What is your favorite color?",
                        {"color": {"type": "string"}},
                        ["color"],
                    )
                },
                request_state="multi-round-2",
            )
        if params.request_state == "multi-round-2":
            return _tool_result([types.TextContent(type="text", text="Multi-round input complete")])
        from mcp.shared.exceptions import MCPError

        raise MCPError(code=-32602, message="Invalid or expired requestState")
    if name == "test_input_required_result_capabilities":
        capabilities: dict[str, Any] = {}
        if isinstance(params.meta, dict):
            value = params.meta.get("io.modelcontextprotocol/clientCapabilities")
            if isinstance(value, dict):
                capabilities = value
        requests: dict[
            str,
            types.CreateMessageRequest | types.ListRootsRequest | types.ElicitRequest,
        ] = {}
        if "sampling" in capabilities:
            requests["sampling"] = _sampling_input("Generate a greeting")
        if "elicitation" in capabilities:
            requests["elicitation"] = _elicitation_input(
                "What is your name?",
                {"name": {"type": "string"}},
                ["name"],
            )
        if "roots" in capabilities:
            requests["roots"] = _roots_input()
        return _input_required(requests)
    if name == "test_header_echo":
        return _tool_result([types.TextContent(type="text", text=str(arguments.get("text", "")))])
    if name == "test_missing_capability":
        return _tool_result([types.TextContent(type="text", text="Sampling capability is present")])
    if name == "test_trigger_tool_change":
        await subscription_bus.publish(ToolsListChanged())
        return _tool_result([types.TextContent(type="text", text="tools changed")])
    if name == "test_trigger_prompt_change":
        await subscription_bus.publish(PromptsListChanged())
        return _tool_result([types.TextContent(type="text", text="prompts changed")])
    if name == "test_simple_text":
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text="This is a simple text response for testing.",
                )
            ]
        )
    if name == "test_image_content":
        return _tool_result(
            [
                types.ImageContent(
                    type="image",
                    data=TEST_IMAGE_BASE64,
                    mime_type="image/png",
                )
            ]
        )
    if name == "test_audio_content":
        return _tool_result(
            [
                types.AudioContent(
                    type="audio",
                    data=TEST_AUDIO_BASE64,
                    mime_type="audio/wav",
                )
            ]
        )
    if name == "test_embedded_resource":
        return _tool_result(
            [
                types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri="test://embedded-resource",
                        mime_type="text/plain",
                        text="This is an embedded resource content.",
                    ),
                )
            ]
        )
    if name == "test_multiple_content_types":
        return _tool_result(
            [
                types.TextContent(type="text", text="Multiple content types test:"),
                types.ImageContent(
                    type="image",
                    data=TEST_IMAGE_BASE64,
                    mime_type="image/png",
                ),
                types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri="test://mixed-content-resource",
                        mime_type="application/json",
                        text=json.dumps({"test": "data", "value": 123}),
                    ),
                ),
            ]
        )
    if name == "test_tool_with_logging":
        for message in (
            "Tool execution started",
            "Tool processing data",
            "Tool execution completed",
        ):
            await ctx.session.send_log_message("info", message)
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text="Tool with logging executed successfully",
                )
            ]
        )
    if name == "test_error_handling":
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text="This tool intentionally returns an error for testing",
                )
            ],
            is_error=True,
        )
    if name == "test_tool_with_progress":
        request_meta = params.meta
        progress_token = (
            request_meta.get("progress_token", request_meta.get("progressToken"))
            if isinstance(request_meta, dict)
            else getattr(request_meta, "progress_token", None)
        )
        if progress_token is not None:
            for progress in (0.0, 50.0, 100.0):
                await ctx.session.send_progress_notification(
                    progress_token,
                    progress,
                    total=100.0,
                    message=f"Completed step {progress:g} of 100",
                )
                if progress < 100:
                    await asyncio.sleep(0.05)
        return _tool_result([types.TextContent(type="text", text=str(progress_token))])
    if name == "test_sampling":
        result = await ctx.session.create_message(
            [
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=str(arguments["prompt"])),
                )
            ],
            max_tokens=100,
        )
        text = getattr(result.content, "text", "No response")
        return _tool_result([types.TextContent(type="text", text=f"LLM response: {text}")])
    if name == "test_elicitation":
        result = await ctx.session.elicit(
            str(arguments["message"]),
            {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            },
        )
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text=f"User response: action={result.action}, content={result.content}",
                )
            ]
        )
    if name == "test_elicitation_sep1034_defaults":
        result = await ctx.session.elicit(
            "Please review and update the form fields with defaults",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "default": "John Doe"},
                    "age": {"type": "integer", "default": 30},
                    "score": {"type": "number", "default": 95.5},
                    "status": {
                        "type": "string",
                        "enum": ["active", "inactive", "pending"],
                        "default": "active",
                    },
                    "verified": {"type": "boolean", "default": True},
                },
            },
        )
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text=f"Elicitation completed: {result.action}",
                )
            ]
        )
    if name == "test_elicitation_sep1330_enums":
        result = await ctx.session.elicit(
            "Please select options from the enum fields",
            {
                "type": "object",
                "properties": {
                    "untitledSingle": {
                        "type": "string",
                        "enum": ["option1", "option2", "option3"],
                    },
                    "titledSingle": {
                        "type": "string",
                        "oneOf": [
                            {"const": "value1", "title": "First Option"},
                            {"const": "value2", "title": "Second Option"},
                        ],
                    },
                    "legacyEnum": {
                        "type": "string",
                        "enum": ["opt1", "opt2"],
                        "enumNames": ["Option One", "Option Two"],
                    },
                    "untitledMulti": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["option1", "option2", "option3"],
                        },
                    },
                    "titledMulti": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"const": "value1", "title": "First Choice"},
                                {"const": "value2", "title": "Second Choice"},
                            ]
                        },
                    },
                },
            },
        )
        return _tool_result(
            [
                types.TextContent(
                    type="text",
                    text=f"Elicitation completed: {result.action}",
                )
            ]
        )
    if name == "json_schema_2020_12_tool":
        return _tool_result([types.TextContent(type="text", text=json.dumps(arguments))])
    return _tool_result(
        [types.TextContent(type="text", text=f"unknown tool: {name}")],
        is_error=True,
    )


async def list_resources(_ctx: Any, _params: Any) -> types.ListResourcesResult:
    return types.ListResourcesResult(
        resources=[
            types.Resource(
                name="static-text",
                uri="test://static-text",
                description="A static text resource for testing.",
                mime_type="text/plain",
            ),
            types.Resource(
                name="static-binary",
                uri="test://static-binary",
                description="A static binary resource for testing.",
                mime_type="image/png",
            ),
            types.Resource(
                name="watched-resource",
                uri="test://watched-resource",
                description="A watched resource for testing.",
                mime_type="text/plain",
            ),
        ]
    )


async def list_resource_templates(
    _ctx: Any,
    _params: Any,
) -> types.ListResourceTemplatesResult:
    return types.ListResourceTemplatesResult(
        resource_templates=[
            types.ResourceTemplate(
                name="template",
                uri_template="test://template/{id}/data",
                description="A resource template for testing.",
                mime_type="application/json",
            )
        ]
    )


async def read_resource(
    _ctx: Any,
    params: types.ReadResourceRequestParams,
) -> types.ReadResourceResult:
    value = params.uri
    if value == "test://static-text":
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=value,
                    text="This is the content of the static text resource.",
                    mime_type="text/plain",
                )
            ]
        )
    if value == "test://static-binary":
        return types.ReadResourceResult(
            contents=[
                types.BlobResourceContents(
                    uri=value,
                    blob=TEST_IMAGE_BASE64,
                    mime_type="image/png",
                )
            ]
        )
    if value == "test://watched-resource":
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=value,
                    text="Watched resource content",
                    mime_type="text/plain",
                )
            ]
        )
    if value.startswith("test://template/") and value.endswith("/data"):
        identifier = value.removeprefix("test://template/").removesuffix("/data")
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=value,
                    text=json.dumps(
                        {
                            "id": identifier,
                            "templateTest": True,
                            "data": f"Data for ID: {identifier}",
                        }
                    ),
                    mime_type="application/json",
                )
            ]
        )
    from mcp.shared.exceptions import MCPError

    raise MCPError(
        code=-32602,
        message="Resource not found",
        data={"uri": value},
    )


async def subscribe_resource(
    _ctx: Any,
    _params: types.SubscribeRequestParams,
) -> types.EmptyResult:
    return types.EmptyResult()


async def unsubscribe_resource(
    _ctx: Any,
    _params: types.UnsubscribeRequestParams,
) -> types.EmptyResult:
    return types.EmptyResult()


async def list_prompts(_ctx: Any, _params: Any) -> types.ListPromptsResult:
    return types.ListPromptsResult(
        prompts=[
            types.Prompt(
                name="test_simple_prompt",
                description="A simple prompt for testing.",
            ),
            types.Prompt(
                name="test_prompt_with_arguments",
                description="A parameterized prompt for testing.",
                arguments=[
                    types.PromptArgument(name="arg1", required=True),
                    types.PromptArgument(name="arg2", required=True),
                ],
            ),
            types.Prompt(
                name="test_prompt_with_embedded_resource",
                description="A prompt with an embedded resource.",
                arguments=[types.PromptArgument(name="resourceUri", required=True)],
            ),
            types.Prompt(
                name="test_prompt_with_image",
                description="A prompt with image content.",
            ),
            types.Prompt(
                name="test_input_required_result_prompt",
                description="A prompt that exercises SEP-2322 MRTR.",
            ),
        ]
    )


async def get_prompt(
    _ctx: Any,
    params: types.GetPromptRequestParams,
) -> types.GetPromptResult | types.InputRequiredResult:
    name = params.name
    arguments = params.arguments or {}
    content: list[types.ContentBlock]
    if name == "test_input_required_result_prompt":
        response = _elicited_content(params.input_responses, "user_context")
        if response is None or not isinstance(response.get("context"), str):
            return _input_required(
                {
                    "user_context": _elicitation_input(
                        "What context should the prompt use?",
                        {"context": {"type": "string"}},
                        ["context"],
                    )
                }
            )
        content = [
            types.TextContent(
                type="text",
                text=f"Prompt context: {response['context']}",
            )
        ]
    elif name == "test_simple_prompt":
        content = [types.TextContent(type="text", text="This is a simple prompt for testing.")]
    elif name == "test_prompt_with_arguments":
        content = [
            types.TextContent(
                type="text",
                text=(
                    f"Prompt with arguments: arg1='{arguments['arg1']}', arg2='{arguments['arg2']}'"
                ),
            )
        ]
    elif name == "test_prompt_with_embedded_resource":
        content = [
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=arguments["resourceUri"],
                    mime_type="text/plain",
                    text="Embedded resource content for testing.",
                ),
            ),
            types.TextContent(type="text", text="Please process the embedded resource above."),
        ]
    elif name == "test_prompt_with_image":
        content = [
            types.ImageContent(
                type="image",
                data=TEST_IMAGE_BASE64,
                mime_type="image/png",
            ),
            types.TextContent(type="text", text="Please analyze the image above."),
        ]
    else:
        raise ValueError(f"unknown prompt: {name}")
    return types.GetPromptResult(
        messages=[types.PromptMessage(role="user", content=item) for item in content]
    )


async def set_logging_level(
    _ctx: Any,
    _params: types.SetLevelRequestParams,
) -> types.EmptyResult:
    return types.EmptyResult()


async def completion(
    _ctx: Any,
    _params: types.CompleteRequestParams,
) -> types.CompleteResult:
    return types.CompleteResult(completion=types.Completion(values=[], total=0, has_more=False))


server = Server(
    "mcp-conformance-test-server",
    version="1.0.0",
    cache_hints={
        "server/discover": CacheHint(ttl_ms=60_000, scope="public"),
        "tools/list": CacheHint(ttl_ms=60_000, scope="public"),
        "prompts/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/read": CacheHint(ttl_ms=1_000, scope="public"),
    },
    on_list_tools=list_tools,
    on_call_tool=call_tool,
    on_list_resources=list_resources,
    on_list_resource_templates=list_resource_templates,
    on_read_resource=read_resource,
    on_subscribe_resource=subscribe_resource,
    on_unsubscribe_resource=unsubscribe_resource,
    on_list_prompts=list_prompts,
    on_get_prompt=get_prompt,
    on_set_logging_level=set_logging_level,
    on_completion=completion,
    on_subscriptions_listen=ListenHandler(subscription_bus),
)
server.middleware.append(
    RequestStateBoundary(
        RequestStateSecurity(
            keys=["hayate-mcp-conformance-request-state-key-v1"],
            bind_principal=None,
        ),
        default_audience=server.name,
    )
)


app = Hayate()
McpMount(
    server,
    tool_capabilities={"test_missing_capability": {"sampling": {}}},
).register(app)
