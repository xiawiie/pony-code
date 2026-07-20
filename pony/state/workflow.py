"""Strict Workflow Mode and Active Plan values owned by Session v3."""

from __future__ import annotations

from copy import deepcopy
import json
import re


WORKFLOW_MODES = frozenset({"plan", "act", "review"})
DEFAULT_WORKFLOW_MODE = "act"
EMPTY_PLAN = {"goal": "", "items": []}
MAX_PLAN_BYTES = 12 * 1024
MAX_PLAN_TEXT_CHARS = 300
MAX_PLAN_ITEMS = 12

_PLAN_FIELDS = frozenset({"goal", "items"})
_ITEM_FIELDS = frozenset({"id", "text", "status"})
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_STATUSES = frozenset({"pending", "in_progress", "completed"})


class PlanValidationError(ValueError):
    code = "invalid_plan"


class SensitivePlanError(PlanValidationError):
    code = "sensitive_content_block"


def validate_workflow_mode(value):
    if not isinstance(value, str) or value not in WORKFLOW_MODES:
        raise ValueError("invalid workflow mode")
    return value


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PlanValidationError("invalid_plan: duplicate JSON key")
        value[key] = item
    return value


def validate_plan(plan, *, redactor=None):
    if not isinstance(plan, dict) or plan.keys() != _PLAN_FIELDS:
        raise PlanValidationError("invalid_plan: plan fields must be goal and items")
    goal = plan.get("goal")
    items = plan.get("items")
    if not isinstance(goal, str) or not isinstance(items, list):
        raise PlanValidationError("invalid_plan: invalid goal or items")
    if plan == EMPTY_PLAN:
        return deepcopy(EMPTY_PLAN)
    if not goal or goal != goal.strip() or len(goal) > MAX_PLAN_TEXT_CHARS:
        raise PlanValidationError("invalid_plan: goal must be 1-300 trimmed characters")
    if _CONTROL_RE.search(goal):
        raise PlanValidationError("invalid_plan: control character")
    if not 1 <= len(items) <= MAX_PLAN_ITEMS:
        raise PlanValidationError("invalid_plan: plan must contain 1-12 items")

    normalized = []
    seen_ids = set()
    in_progress = 0
    for item in items:
        if not isinstance(item, dict) or item.keys() != _ITEM_FIELDS:
            raise PlanValidationError("invalid_plan: invalid item fields")
        item_id = item.get("id")
        text = item.get("text")
        status = item.get("status")
        if not isinstance(item_id, str) or _ITEM_ID_RE.fullmatch(item_id) is None:
            raise PlanValidationError("invalid_plan: invalid item id")
        if item_id in seen_ids:
            raise PlanValidationError("invalid_plan: duplicate item id")
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or len(text) > MAX_PLAN_TEXT_CHARS
        ):
            raise PlanValidationError("invalid_plan: item text must be 1-300 trimmed characters")
        if _CONTROL_RE.search(text) or _CONTROL_RE.search(item_id):
            raise PlanValidationError("invalid_plan: control character")
        if not isinstance(status, str) or status not in _STATUSES:
            raise PlanValidationError("invalid_plan: invalid item status")
        seen_ids.add(item_id)
        in_progress += status == "in_progress"
        normalized.append({"id": item_id, "text": text, "status": status})
    if in_progress > 1:
        raise PlanValidationError("invalid_plan: multiple in_progress items")
    validated = {"goal": goal, "items": normalized}
    if redactor is not None and redactor(deepcopy(validated)) != validated:
        raise SensitivePlanError("sensitive_content_block")
    if len(
        json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ) > MAX_PLAN_BYTES:
        raise PlanValidationError("invalid_plan: plan exceeds 12 KiB")
    return validated


def parse_plan_json(plan_json, *, redactor=None):
    if not isinstance(plan_json, str):
        raise PlanValidationError("invalid_plan: plan_json must be a string")
    try:
        raw = plan_json.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanValidationError("invalid_plan: plan_json must be UTF-8") from None
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanValidationError("invalid_plan: plan exceeds 12 KiB")
    try:
        value = json.loads(plan_json, object_pairs_hook=_object_from_pairs)
    except PlanValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise PlanValidationError("invalid_plan: malformed JSON") from None
    return validate_plan(value, redactor=redactor)
