import pytest

from pony.providers.openai_wire import (
    drop_optional_null_arguments,
    prepare_function_tools,
    render_system_instructions,
)


def _tool_schema():
    return {
        "name": "search",
        "description": "Search files",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    }


def test_shared_function_schema_is_wire_envelope_neutral():
    original = _tool_schema()

    prepared, optional_by_name = prepare_function_tools([original], strict=True)

    assert prepared == [
        {
            "name": "search",
            "description": "Search files",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                },
                "required": ["pattern", "path"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    assert optional_by_name == {"search": {"path"}}
    assert original == _tool_schema()
    assert "type" not in prepared[0]
    assert "function" not in prepared[0]


def test_shared_function_schema_keeps_protocol_core_conservative():
    prepared, optional_by_name = prepare_function_tools([_tool_schema()], strict=False)

    assert "strict" not in prepared[0]
    assert prepared[0]["parameters"]["required"] == ["pattern"]
    assert prepared[0]["parameters"]["additionalProperties"] is False
    assert drop_optional_null_arguments(
        {"pattern": "x", "path": None},
        optional_by_name["search"],
    ) == {"pattern": "x"}


def test_shared_system_instructions_validate_canonical_blocks():
    assert render_system_instructions(
        [
            {"type": "text", "text": "first"},
            {"type": "text", "text": ""},
            {"type": "text", "text": "second"},
        ]
    ) == "first\n\nsecond"

    with pytest.raises(ValueError, match="system block must be text"):
        render_system_instructions([{"type": "image", "text": "opaque"}])
