"""Shared wire primitives for OpenAI-compatible provider adapters."""

from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version


try:
    OPENAI_USER_AGENT = f"pony/{version('pony-code')}"
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree.
    OPENAI_USER_AGENT = "pony/unknown"


def _closed_object_schema(schema):
    copied = deepcopy(schema)
    if not isinstance(copied, dict):
        raise ValueError("tool parameters must be an object")
    if copied.get("type") == "object":
        copied["additionalProperties"] = False
        properties = copied.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("tool properties must be an object")
        copied["properties"] = {
            name: _closed_object_schema(value) for name, value in properties.items()
        }
    if copied.get("type") == "array" and "items" in copied:
        copied["items"] = _closed_object_schema(copied["items"])
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in copied:
            values = copied[keyword]
            if not isinstance(values, list):
                raise ValueError("schema alternatives must be a list")
            copied[keyword] = [_closed_object_schema(value) for value in values]
    return copied


def prepare_function_tools(tools, *, strict):
    """Normalize canonical tool schemas before an adapter adds its wire envelope."""
    prepared = []
    optional_by_name = {}
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            raise ValueError("tool must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be text")
        parameters = _closed_object_schema(tool.get("input_schema") or {})
        required = parameters.get("required", [])
        properties = parameters.get("properties", {})
        if not isinstance(required, list) or not all(
            isinstance(value, str) for value in required
        ):
            raise ValueError("tool required must be a list of names")
        optional = set(properties) - set(required)
        optional_by_name[name] = optional
        if strict:
            for argument in sorted(optional):
                properties[argument] = {
                    "anyOf": [properties[argument], {"type": "null"}]
                }
            parameters["required"] = list(properties)
        item = {
            "name": name,
            "description": str(tool.get("description", "") or ""),
            "parameters": parameters,
        }
        if strict:
            item["strict"] = True
        prepared.append(item)
    return prepared, optional_by_name


def render_system_instructions(system):
    """Validate canonical system blocks and join them for OpenAI wire formats."""
    if not isinstance(system, list):
        raise ValueError("system must be a list")
    parts = []
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ValueError("system block must be text")
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError("system text must be text")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def drop_optional_null_arguments(arguments, optional_arguments):
    """Restore omitted optional arguments after strict-schema nullable encoding."""
    for argument in optional_arguments:
        if arguments.get(argument) is None:
            arguments.pop(argument, None)
    return arguments
