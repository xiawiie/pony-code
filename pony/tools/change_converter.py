"""Explicit converter for legacy Tool Change records.

Runtime readers remain current-only; migration callers invoke this converter
before transactional cutover.
"""

from copy import deepcopy

from pony.state.checkpoint_store import _TOOL_CHANGE_FIELDS, _validate_tool_change_record
from pony.recovery.models import TOOL_CHANGE_FORMAT_VERSION


_TOOL_CHANGE_V1_FIELDS = _TOOL_CHANGE_FIELDS - {"policy", "sandbox"}
_TOOL_CHANGE_V1_STATUSES = frozenset(
    {"pending", "finalized", "error", "partial_success", "interrupted"}
)


def convert_tool_change_v1(record):
    if not isinstance(record, dict) or record.keys() != _TOOL_CHANGE_V1_FIELDS:
        raise ValueError("invalid_tool_change_record")
    if record.get("record_type") != "tool_change":
        raise ValueError("invalid_tool_change_record")
    if type(record.get("format_version")) is not int or record["format_version"] != 1:
        raise ValueError("unsupported_tool_change_format")
    if record.get("status") not in _TOOL_CHANGE_V1_STATUSES:
        raise ValueError("invalid_tool_change_status")
    converted = deepcopy(record)
    converted["format_version"] = TOOL_CHANGE_FORMAT_VERSION
    converted["policy"] = {}
    converted["sandbox"] = {}
    _validate_tool_change_record(converted)
    converted["status"] = "legacy_migrated"
    converted["policy"] = {
        "schema_version": 1,
        "decision": "allow",
        "reason_code": "legacy_migrated",
        "effect_class": record.get("effect_class", "workspace_write"),
        "risk_class": "legacy",
        "evidence_complete": False,
        "approval": deepcopy(record.get("approval", {})),
    }
    return converted
