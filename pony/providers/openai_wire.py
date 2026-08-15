"""Shared wire primitives for OpenAI-compatible provider adapters."""

from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version


try:
    OPENAI_USER_AGENT = f"pony/{version('pony-code')}"
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree.
    OPENAI_USER_AGENT = "pony/unknown"


_ARRAY_ITEM = "[]"


def _normalized_schema(schema, *, strict, path, optional_paths):
    if not isinstance(schema, dict):
        raise ValueError("tool parameters must be an object")
    if strict:
        schema.pop("default", None)
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("tool properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(value, str) for value in required
        ):
            raise ValueError("tool required must be a list of names")
        required_names = set(required)
        for name, value in list(properties.items()):
            property_path = (*path, name)
            normalized = _normalized_schema(
                value,
                strict=strict,
                path=property_path,
                optional_paths=optional_paths,
            )
            if name not in required_names:
                optional_paths.add(property_path)
                if strict:
                    normalized = {"anyOf": [normalized, {"type": "null"}]}
            properties[name] = normalized
        if strict:
            schema["required"] = list(properties)
    if schema.get("type") == "array" and "items" in schema:
        schema["items"] = _normalized_schema(
            schema["items"],
            strict=strict,
            path=(*path, _ARRAY_ITEM),
            optional_paths=optional_paths,
        )
    if strict and any(keyword in schema for keyword in ("oneOf", "allOf")):
        raise ValueError("strict tool schema uses an unsupported composition")
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            values = schema[keyword]
            if not isinstance(values, list):
                raise ValueError("schema alternatives must be a list")
            schema[keyword] = [
                _normalized_schema(
                    value,
                    strict=strict,
                    path=path,
                    optional_paths=optional_paths,
                )
                for value in values
            ]
    return schema


def _prepare_schema(schema, *, strict):
    optional_paths = set()
    normalized = _normalized_schema(
        deepcopy(schema),
        strict=strict,
        path=(),
        optional_paths=optional_paths,
    )
    if strict and (
        normalized.get("type") != "object" or "anyOf" in normalized
    ):
        raise ValueError("strict tool schema root must be an object")
    return normalized, optional_paths


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
        parameters, optional_paths = _prepare_schema(
            tool.get("input_schema") or {},
            strict=strict,
        )
        optional_by_name[name] = optional_paths
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


def _drop_optional_null_path(value, path):
    if not path:
        return
    segment, *remaining = path
    if segment == _ARRAY_ITEM:
        if isinstance(value, list):
            for item in value:
                _drop_optional_null_path(item, remaining)
        return
    if not isinstance(value, dict) or segment not in value:
        return
    if not remaining:
        if value.get(segment) is None:
            value.pop(segment, None)
        return
    _drop_optional_null_path(value[segment], remaining)


def drop_optional_null_arguments(arguments, optional_arguments):
    """Restore omitted optional arguments after strict-schema nullable encoding."""
    for argument in optional_arguments:
        path = (argument,) if isinstance(argument, str) else tuple(argument)
        _drop_optional_null_path(arguments, path)
    return arguments
