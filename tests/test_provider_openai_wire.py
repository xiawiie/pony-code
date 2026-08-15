import pytest

from pony.agent.context_manager import _build_tools_list
from pony.providers.openai_wire import (
    drop_optional_null_arguments,
    prepare_function_tools,
    render_system_instructions,
)
from pony.tools.registry import WORKTREE_DELEGATE_TOOL_SPEC


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
    assert optional_by_name == {"search": {("path",)}}
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


def test_strict_function_schema_recurses_through_objects_arrays_and_any_of():
    tool = {
        "name": "delegate_worktrees",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "mode": {"type": "string", "default": "readonly"},
                            "options": {
                                "anyOf": [
                                    {
                                        "type": "object",
                                        "properties": {"branch": {"type": "string"}},
                                    },
                                    {"type": "string"},
                                ]
                            },
                        },
                        "required": ["name"],
                    },
                }
            },
            "required": ["tasks"],
        },
    }

    prepared, optional_by_name = prepare_function_tools([tool], strict=True)

    task_schema = prepared[0]["parameters"]["properties"]["tasks"]["items"]
    assert task_schema["required"] == ["name", "mode", "options"]
    assert task_schema["additionalProperties"] is False
    assert task_schema["properties"]["mode"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    options_schema = task_schema["properties"]["options"]["anyOf"][0]
    option_object = options_schema["anyOf"][0]
    assert option_object["required"] == ["branch"]
    assert option_object["additionalProperties"] is False
    assert optional_by_name == {
        "delegate_worktrees": {
            ("tasks", "[]", "mode"),
            ("tasks", "[]", "options"),
            ("tasks", "[]", "options", "branch"),
        }
    }

    arguments = {
        "tasks": [
            {"name": "one", "mode": None, "options": {"branch": None}},
            {"name": "two", "mode": "write", "options": None},
        ]
    }
    assert drop_optional_null_arguments(
        arguments,
        optional_by_name["delegate_worktrees"],
    ) == {
        "tasks": [
            {"name": "one", "options": {}},
            {"name": "two", "mode": "write"},
        ]
    }


@pytest.mark.parametrize("keyword", ("oneOf", "allOf"))
def test_strict_function_schema_rejects_unsupported_composition(keyword):
    tool = _tool_schema()
    tool["input_schema"]["properties"]["path"] = {
        keyword: [{"type": "string"}, {"type": "null"}]
    }

    with pytest.raises(ValueError, match="unsupported composition"):
        prepare_function_tools([tool], strict=True)


def test_strict_function_schema_rejects_root_any_of():
    tool = _tool_schema()
    tool["input_schema"] = {
        "anyOf": [{"type": "object"}, {"type": "object"}]
    }

    with pytest.raises(ValueError, match="root must be an object"):
        prepare_function_tools([tool], strict=True)


def test_strict_function_schema_removes_defaults_from_real_worktree_tool():
    canonical = _build_tools_list(
        {"delegate_worktrees": WORKTREE_DELEGATE_TOOL_SPEC}
    )

    prepared, _optional_by_name = prepare_function_tools(canonical, strict=True)

    task = prepared[0]["parameters"]["properties"]["tasks"]["items"]
    assert "default" not in task["properties"]["mode"]["anyOf"][0]
    assert "default" not in task["properties"]["max_steps"]["anyOf"][0]


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
