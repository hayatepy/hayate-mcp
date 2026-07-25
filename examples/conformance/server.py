"""Official MCP conformance fixture mounted through hayate-mcp.

Names intentionally match ``modelcontextprotocol/conformance``. This is test
infrastructure, not a recommended application surface.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import mcp.types as types
from hayate import Hayate
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents

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
    },
    "additionalProperties": False,
}
EMPTY_SCHEMA = {"type": "object", "properties": {}}

server = Server("mcp-conformance-test-server", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=description, inputSchema=schema)
        for name, description, schema in (
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
            ("test_elicitation_sep1034_defaults", "Tests elicitation defaults.", EMPTY_SCHEMA),
            ("test_elicitation_sep1330_enums", "Tests elicitation enums.", EMPTY_SCHEMA),
            (
                "json_schema_2020_12_tool",
                "Tool with JSON Schema 2020-12 features.",
                JSON_SCHEMA_2020_12,
            ),
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
    if name == "test_simple_text":
        return [
            types.TextContent(
                type="text",
                text="This is a simple text response for testing.",
            )
        ]
    if name == "test_image_content":
        return [types.ImageContent(type="image", data=TEST_IMAGE_BASE64, mimeType="image/png")]
    if name == "test_audio_content":
        return [types.AudioContent(type="audio", data=TEST_AUDIO_BASE64, mimeType="audio/wav")]
    if name == "test_embedded_resource":
        return [
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="test://embedded-resource",
                    mimeType="text/plain",
                    text="This is an embedded resource content.",
                ),
            )
        ]
    if name == "test_multiple_content_types":
        return [
            types.TextContent(type="text", text="Multiple content types test:"),
            types.ImageContent(type="image", data=TEST_IMAGE_BASE64, mimeType="image/png"),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="test://mixed-content-resource",
                    mimeType="application/json",
                    text=json.dumps({"test": "data", "value": 123}),
                ),
            ),
        ]
    if name == "test_tool_with_logging":
        for message in (
            "Tool execution started",
            "Tool processing data",
            "Tool execution completed",
        ):
            await server.request_context.session.send_log_message("info", message)
        return [types.TextContent(type="text", text="Tool with logging executed successfully")]
    if name == "test_error_handling":
        raise RuntimeError("This tool intentionally returns an error for testing")
    if name == "test_tool_with_progress":
        meta = server.request_context.meta
        progress_token = None if meta is None else meta.progressToken
        if progress_token is not None:
            for progress in (0.0, 50.0, 100.0):
                await server.request_context.session.send_progress_notification(
                    progress_token,
                    progress,
                    total=100.0,
                    message=f"Completed step {progress:g} of 100",
                )
                if progress < 100:
                    await asyncio.sleep(0.05)
        return [types.TextContent(type="text", text=str(progress_token))]
    if name == "test_sampling":
        result = await server.request_context.session.create_message(
            [
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=str(arguments["prompt"])),
                )
            ],
            max_tokens=100,
        )
        text = getattr(result.content, "text", "No response")
        return [types.TextContent(type="text", text=f"LLM response: {text}")]
    if name == "test_elicitation":
        result = await server.request_context.session.elicit(
            str(arguments["message"]),
            {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            },
        )
        return [
            types.TextContent(
                type="text",
                text=f"User response: action={result.action}, content={result.content}",
            )
        ]
    if name == "test_elicitation_sep1034_defaults":
        result = await server.request_context.session.elicit(
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
        return [types.TextContent(type="text", text=f"Elicitation completed: {result.action}")]
    if name == "test_elicitation_sep1330_enums":
        result = await server.request_context.session.elicit(
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
        return [types.TextContent(type="text", text=f"Elicitation completed: {result.action}")]
    if name == "json_schema_2020_12_tool":
        return [types.TextContent(type="text", text=json.dumps(arguments))]
    raise ValueError(f"unknown tool: {name}")


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            name="static-text",
            uri="test://static-text",
            description="A static text resource for testing.",
            mimeType="text/plain",
        ),
        types.Resource(
            name="static-binary",
            uri="test://static-binary",
            description="A static binary resource for testing.",
            mimeType="image/png",
        ),
        types.Resource(
            name="watched-resource",
            uri="test://watched-resource",
            description="A watched resource for testing.",
            mimeType="text/plain",
        ),
    ]


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            name="template",
            uriTemplate="test://template/{id}/data",
            description="A resource template for testing.",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def read_resource(uri: Any) -> list[ReadResourceContents]:
    value = str(uri)
    if value == "test://static-text":
        return [
            ReadResourceContents(
                "This is the content of the static text resource.",
                "text/plain",
            )
        ]
    if value == "test://static-binary":
        return [ReadResourceContents(base64.b64decode(TEST_IMAGE_BASE64), "image/png")]
    if value == "test://watched-resource":
        return [ReadResourceContents("Watched resource content", "text/plain")]
    if value.startswith("test://template/") and value.endswith("/data"):
        identifier = value.removeprefix("test://template/").removesuffix("/data")
        return [
            ReadResourceContents(
                json.dumps(
                    {
                        "id": identifier,
                        "templateTest": True,
                        "data": f"Data for ID: {identifier}",
                    }
                ),
                "application/json",
            )
        ]
    raise ValueError(f"unknown resource: {value}")


@server.subscribe_resource()
async def subscribe_resource(_uri: Any) -> None:
    return None


@server.unsubscribe_resource()
async def unsubscribe_resource(_uri: Any) -> None:
    return None


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(name="test_simple_prompt", description="A simple prompt for testing."),
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
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    arguments = arguments or {}
    if name == "test_simple_prompt":
        content: list[types.ContentBlock] = [
            types.TextContent(type="text", text="This is a simple prompt for testing.")
        ]
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
                    mimeType="text/plain",
                    text="Embedded resource content for testing.",
                ),
            ),
            types.TextContent(type="text", text="Please process the embedded resource above."),
        ]
    elif name == "test_prompt_with_image":
        content = [
            types.ImageContent(type="image", data=TEST_IMAGE_BASE64, mimeType="image/png"),
            types.TextContent(type="text", text="Please analyze the image above."),
        ]
    else:
        raise ValueError(f"unknown prompt: {name}")
    return types.GetPromptResult(
        messages=[types.PromptMessage(role="user", content=item) for item in content]
    )


@server.set_logging_level()
async def set_logging_level(_level: types.LoggingLevel) -> None:
    return None


@server.completion()
async def completion(
    _reference: types.PromptReference | types.ResourceTemplateReference,
    _argument: types.CompletionArgument,
    _context: types.CompletionContext | None,
) -> types.Completion:
    return types.Completion(values=[], total=0, hasMore=False)


app = Hayate()
McpMount(server).register(app)
