"""Minimal permission modes and fail-closed tool policy."""

from enum import Enum


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    DONT_ASK = "dontAsk"


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_MUTATING_EFFECTS = frozenset({"workspace_write", "memory_write", "session_state"})


def decide_permission(
    *,
    project_trusted,
    mode,
    effect_class,
    explicit=None,
    builtin_edit=False,
):
    """Return allow, ask, or deny without inferring command safety."""
    try:
        mode = PermissionMode(mode)
        explicit = None if explicit is None else PermissionDecision(explicit)
    except (TypeError, ValueError):
        return PermissionDecision.DENY

    if project_trusted is not True or explicit is PermissionDecision.DENY:
        return PermissionDecision.DENY
    if effect_class not in _MUTATING_EFFECTS | {"read_only"}:
        return PermissionDecision.DENY

    mutating = effect_class in _MUTATING_EFFECTS
    if mode is PermissionMode.PLAN and mutating:
        return PermissionDecision.DENY
    if explicit is PermissionDecision.ALLOW:
        return PermissionDecision.ALLOW
    if explicit is PermissionDecision.ASK:
        return (
            PermissionDecision.DENY
            if mode is PermissionMode.DONT_ASK
            else PermissionDecision.ASK
        )
    if not mutating:
        return PermissionDecision.ALLOW
    if (
        mode is PermissionMode.ACCEPT_EDITS
        and effect_class == "workspace_write"
        and builtin_edit is True
    ):
        return PermissionDecision.ALLOW
    if mode is PermissionMode.DONT_ASK:
        return PermissionDecision.DENY
    return PermissionDecision.ASK
