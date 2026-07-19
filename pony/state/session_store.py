"""Append-only JSONL Session Tree with legacy JSON migration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import warnings

from pony.state import file_lock
from pony.context.escaping import escape_pony_tags
from pony.agent.messages import (
    MessageValidationError,
    message_content_text,
    validate_messages,
)
from pony.agent.model_capabilities import estimate_text_tokens
from pony.security.private_files import (
    append_private_bytes,
    ensure_private_dir,
    ensure_private_file,
    harden_private_tree,
    private_file_signature,
    private_directory_identity,
    read_private_bytes,
    write_private_bytes_atomic,
)
from pony.security.paths import require_regular_no_symlink
from pony.state.workflow import (
    DEFAULT_WORKFLOW_MODE,
    EMPTY_PLAN,
    PlanValidationError,
    parse_plan_json,
    validate_plan,
    validate_workflow_mode,
)
from pony.tools.permissions import PermissionMode, validate_permission_mode
from pony.workspace.context import now


SESSION_RECORD_TYPE = "session"
SESSION_FORMAT_VERSION = 5
PREVIOUS_SESSION_FORMAT_VERSION = 4
WORKFLOW_SESSION_FORMAT_VERSION = 3
LEGACY_JSONL_SESSION_FORMAT_VERSION = 2
LEGACY_SESSION_FORMAT_VERSION = 1
SESSION_HEADER_RECORD_TYPE = "session_header"
SESSION_ENTRY_RECORD_TYPE = "session_entry"
MAX_SESSION_ENTRY_BYTES = 8 * 1024 * 1024
SESSION_SOFT_LIMIT_BYTES = 128 * 1024 * 1024
MAX_SESSION_BYTES = 512 * 1024 * 1024
MAX_PLAN_TEXT_BYTES = 12 * 1024

ENTRY_TYPES = frozenset(
    {
        "message",
        "tool_exchange",
        "permission_mode_change",
        "plan_artifact",
        "compaction",
        "branch_summary",
        "task_checkpoint",
        "label",
        "rewind",
        "session_info",
        "context_recovery",
        "migration",
    }
)
_V3_ENTRY_TYPES = (ENTRY_TYPES - {"permission_mode_change", "plan_artifact"}) | {
    "workflow_mode_change",
    "plan_update",
}
_V2_ENTRY_TYPES = (ENTRY_TYPES - {"permission_mode_change", "plan_artifact"}) | {
    "model_change"
}


class SessionFormatError(ValueError):
    """A Session artifact does not match the current on-disk contract."""


class PlanApprovalChanged(SessionFormatError):
    code = "plan_approval_changed"


class SessionMigrationRequired(SessionFormatError):
    code = "session_migration_required"

    def __init__(self, session_id):
        super().__init__(f"session_migration_required: resume session {session_id} first")


class UnsupportedLegacyEntry(SessionFormatError):
    code = "unsupported_legacy_entry"

    def __init__(self, kind):
        super().__init__(f"unsupported_legacy_entry: {kind}")


class SessionTailRepairRequired(SessionFormatError):
    """The final JSONL line is incomplete and needs explicit repair."""

    code = "session_tail_repair_required"

    def __init__(self, session_id, valid_bytes):
        super().__init__(
            f"session_tail_repair_required: {session_id} has an incomplete final line; "
            "run explicit tail repair"
        )
        self.session_id = session_id
        self.valid_bytes = int(valid_bytes)


@dataclass(frozen=True)
class SessionTree:
    header: dict
    entries: tuple[dict, ...]
    active_path: tuple[dict, ...]
    projection: dict
    entry_token_estimates: dict

    @property
    def leaf_id(self):
        return self.entries[-1]["id"] if self.entries else ""


@dataclass(frozen=True)
class SessionContextView:
    """The active model-visible history derived from one Session Tree path."""

    messages: tuple[dict, ...]
    message_entries: tuple[dict, ...]
    summary: str = ""
    split_turn_summary: str = ""
    branch_summary: str = ""
    summary_tokens: int = 0
    split_turn_summary_tokens: int = 0
    branch_summary_tokens: int = 0
    tail_tokens: int = 0
    tokens_before: int = 0
    first_kept_entry_id: str = ""
    compaction_entry_id: str = ""
    compaction_reason: str = "not_compacted"
    compression_ratio: float = 1.0
    branch_summary_entry_id: str = ""
    canonical_message_count: int = 0
    canonical_last_message: dict | None = None


def _identity(value):
    return value


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENTRY_ID_RE = re.compile(r"^[a-f0-9]{16,32}$")
_REQUIRED_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "id",
        "created_at",
        "workspace_root",
        "messages",
        "working_memory",
        "memory",
        "recently_recalled",
        "checkpoints",
        "resume_state",
        "runtime_identity",
        "permission_mode",
    }
)
_V4_REQUIRED_FIELDS = _REQUIRED_FIELDS | {"recovery"}
_PREVIOUS_REQUIRED_FIELDS = (_V4_REQUIRED_FIELDS - {"permission_mode"}) | {
    "workflow_mode",
    "active_plan",
}
_LEGACY_REQUIRED_FIELDS = _REQUIRED_FIELDS - {"permission_mode"}
_OPTIONAL_FIELDS = frozenset(
    {
        "provider_binding",
        "permission_rules",
        "plan_text",
        "plan_revision",
        "pre_plan_mode",
    }
)
_LEGACY_OPTIONAL_FIELDS = _OPTIONAL_FIELDS | {"recovery"}
_PROTOCOL_FAMILIES = {
    "anthropic_messages",
    "openai_chat_completions",
    "openai_responses",
    "ollama_chat",
}
_DICT_FIELDS = (
    "working_memory",
    "memory",
    "checkpoints",
    "resume_state",
    "runtime_identity",
)
_DERIVED_CACHE_FIELDS = frozenset({"working_memory", "memory", "recently_recalled"})
_HEADER_PROJECTION_FIELDS = frozenset(
    {"record_type", "format_version", "id", "created_at", "workspace_root"}
)
_SESSION_INFO_FIELDS = (
    (_REQUIRED_FIELDS | _OPTIONAL_FIELDS)
    - {
        "messages",
        "permission_mode",
        "plan_text",
        "plan_revision",
        "pre_plan_mode",
    }
    - _DERIVED_CACHE_FIELDS
)
_V4_SESSION_INFO_FIELDS = _SESSION_INFO_FIELDS | {"recovery"}
_MUTABLE_SESSION_INFO_FIELDS = frozenset(
    {"resume_state", "runtime_identity", "permission_rules"}
)
_TASK_CHECKPOINT_FIELDS = frozenset(
    {
        "checkpoint_id",
        "parent_checkpoint_id",
        "created_at",
        "goal",
        "status",
        "current_goal",
        "completed",
        "in_progress",
        "excluded",
        "blocker",
        "current_blocker",
        "next_steps",
        "next_step",
        "key_files",
        "read_files",
        "modified_files",
        "worktree_identity_digest",
        "context_usage",
        "label",
        "trigger",
        "freshness",
        "summary",
        "runtime_identity",
    }
)
_V4_TASK_CHECKPOINT_FIELDS = _TASK_CHECKPOINT_FIELDS | {"workspace_checkpoint_id"}
_FEATURE_FLAG_FIELDS = frozenset({"memory"})
_HEADER_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "id",
        "created_at",
        "workspace_root",
        "worktree_identity",
    }
)
_WORKTREE_FIELDS = frozenset(
    {
        "lexical_root",
        "git_common_dir",
        "git_dir",
        "root_device",
        "root_inode",
        "digest",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "id",
        "parent_id",
        "timestamp",
        "type",
        "data",
    }
)


def _session_id(value):
    session_id = value if isinstance(value, str) else ""
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid session id")
    return session_id


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SessionFormatError("duplicate session key")
        value[key] = item
    return value


def _decode_json_object(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object_from_pairs)
    except SessionFormatError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SessionFormatError("failed to decode session") from None


def _validate_projection(payload, session_id, *, version=SESSION_FORMAT_VERSION):
    if not isinstance(payload, dict):
        raise SessionFormatError("session payload must be an object")
    if version == SESSION_FORMAT_VERSION:
        required = _REQUIRED_FIELDS
    elif version == PREVIOUS_SESSION_FORMAT_VERSION:
        required = _V4_REQUIRED_FIELDS
    elif version == WORKFLOW_SESSION_FORMAT_VERSION:
        required = _PREVIOUS_REQUIRED_FIELDS
    else:
        required = _LEGACY_REQUIRED_FIELDS
    optional = (
        _LEGACY_OPTIONAL_FIELDS
        if version == LEGACY_SESSION_FORMAT_VERSION
        or version == LEGACY_JSONL_SESSION_FORMAT_VERSION
        else _OPTIONAL_FIELDS
    )
    if not required <= payload.keys() or not payload.keys() <= (required | optional):
        raise SessionFormatError("session payload fields do not match current format")
    if payload.get("record_type") != SESSION_RECORD_TYPE:
        raise SessionFormatError("invalid session record type")
    if (
        type(payload.get("format_version")) is not int
        or payload["format_version"] != version
    ):
        raise SessionFormatError("invalid session format version")
    if payload.get("id") != session_id:
        raise SessionFormatError("session id does not match file name")
    if any(
        not isinstance(payload.get(key), str)
        for key in ("id", "created_at", "workspace_root")
    ):
        raise SessionFormatError("invalid session string field")
    dict_fields = _DICT_FIELDS
    if version == PREVIOUS_SESSION_FORMAT_VERSION or (
        version in {LEGACY_SESSION_FORMAT_VERSION, LEGACY_JSONL_SESSION_FORMAT_VERSION}
        and "recovery" in payload
    ):
        dict_fields += ("recovery",)
    elif version == WORKFLOW_SESSION_FORMAT_VERSION:
        dict_fields += ("recovery", "active_plan")
    if any(not isinstance(payload.get(key), dict) for key in dict_fields):
        raise SessionFormatError("invalid session object field")
    if not isinstance(payload.get("recently_recalled"), list):
        raise SessionFormatError("invalid session list field")
    if version == SESSION_FORMAT_VERSION:
        try:
            validate_permission_mode(payload.get("permission_mode"))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from None
        plan_text = payload.get("plan_text", "")
        plan_revision = payload.get("plan_revision", 0)
        pre_plan_mode = payload.get("pre_plan_mode", "")
        if (
            not isinstance(plan_text, str)
            or len(plan_text.encode("utf-8")) > MAX_PLAN_TEXT_BYTES
            or type(plan_revision) is not int
            or plan_revision < 0
            or (plan_revision == 0) != (plan_text == "")
            or not isinstance(pre_plan_mode, str)
        ):
            raise SessionFormatError("invalid plan state")
        if pre_plan_mode:
            try:
                validate_permission_mode(pre_plan_mode)
            except ValueError as exc:
                raise SessionFormatError(str(exc)) from None
        rules = payload.get(
            "permission_rules",
            {"allow": [], "ask": [], "deny": []},
        )
        if (
            not isinstance(rules, dict)
            or rules.keys() != {"allow", "ask", "deny"}
            or any(not isinstance(rules[key], list) for key in rules)
            or any(
                not isinstance(tool, str) or not tool
                for values in rules.values()
                for tool in values
            )
            or len({tool for values in rules.values() for tool in values})
            != sum(len(values) for values in rules.values())
        ):
            raise SessionFormatError("invalid permission rules")
    elif version == WORKFLOW_SESSION_FORMAT_VERSION:
        try:
            validate_workflow_mode(payload.get("workflow_mode"))
            validate_plan(payload.get("active_plan"))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from None
    binding = payload.get("provider_binding")
    if binding is not None:
        _validate_provider_binding(binding)
    identities = [payload["runtime_identity"]]
    items = payload["checkpoints"].get("items", {})
    if isinstance(items, dict):
        for checkpoint in items.values():
            if not isinstance(checkpoint, dict):
                raise SessionFormatError("invalid embedded checkpoint")
            fields = (
                _V4_TASK_CHECKPOINT_FIELDS
                if version in {PREVIOUS_SESSION_FORMAT_VERSION, WORKFLOW_SESSION_FORMAT_VERSION}
                else _TASK_CHECKPOINT_FIELDS
            )
            if not checkpoint.keys() <= fields:
                raise SessionFormatError("invalid embedded checkpoint fields")
            identity = checkpoint.get("runtime_identity")
            if isinstance(identity, dict):
                identities.append(identity)
    for identity in identities:
        feature_flags = identity.get("feature_flags", {})
        if not isinstance(feature_flags, dict):
            raise SessionFormatError("invalid runtime identity feature flags")
        if not feature_flags.keys() <= _FEATURE_FLAG_FIELDS or any(
            type(value) is not bool for value in feature_flags.values()
        ):
            raise SessionFormatError("unsupported runtime identity feature flag")
    try:
        validate_messages(payload.get("messages"), require_meta=True)
    except MessageValidationError as exc:
        raise SessionFormatError(str(exc)) from None
    return payload


def _validate_session_info_updates(values):
    if (
        not isinstance(values, dict)
        or not values.keys() <= _MUTABLE_SESSION_INFO_FIELDS
    ):
        raise SessionFormatError("invalid mutable session info fields")
    if any(not isinstance(value, dict) for value in values.values()):
        raise SessionFormatError("invalid mutable session info value")
    identity = values.get("runtime_identity")
    if identity is not None:
        feature_flags = identity.get("feature_flags", {})
        if not isinstance(feature_flags, dict):
            raise SessionFormatError("invalid runtime identity feature flags")
        if not feature_flags.keys() <= _FEATURE_FLAG_FIELDS or any(
            type(value) is not bool for value in feature_flags.values()
        ):
            raise SessionFormatError("unsupported runtime identity feature flag")
    return values


def _validate_provider_binding(binding):
    if (
        not isinstance(binding, dict)
        or binding.keys() != {"protocol_family", "model", "endpoint_hash"}
        or any(not isinstance(value, str) or not value for value in binding.values())
        or binding["protocol_family"] not in _PROTOCOL_FAMILIES
        or re.fullmatch(r"sha256:[0-9a-f]{64}", binding["endpoint_hash"]) is None
    ):
        raise SessionFormatError("invalid provider binding")
    return binding


def _validate_legacy_payload(payload, session_id):
    return _validate_projection(
        payload,
        session_id,
        version=LEGACY_SESSION_FORMAT_VERSION,
    )


def _lexical_absolute(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def _read_small_regular(path, max_bytes=4096):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SessionFormatError("unsafe Git identity file")
    if before.st_size > max_bytes:
        raise SessionFormatError("Git identity file too large")
    with path.open("rb") as file:
        raw = file.read(max_bytes + 1)
    after = path.lstat()
    if len(raw) > max_bytes or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        raise SessionFormatError("Git identity file changed")
    return raw.decode("utf-8").strip()


def _stable_private_read(
    path,
    *,
    root,
    root_identity,
    max_bytes,
    error,
    expected_signature=None,
    expected_bytes=None,
):
    before = private_file_signature(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    if expected_signature is not None and before != expected_signature:
        raise SessionFormatError(error)
    raw = read_private_bytes(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
        max_bytes=max_bytes,
        harden=False,
    )
    after = private_file_signature(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    if before != after or (expected_bytes is not None and raw != expected_bytes):
        raise SessionFormatError(error)
    return raw, after


def _harden_migration_source(path, *, root, root_identity):
    signature = private_file_signature(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    if signature[6] != 0o600:
        ensure_private_file(
            path,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )


def _write_or_verify_backup(path, raw, *, root, root_identity, max_bytes):
    if not path.exists():
        write_private_bytes_atomic(
            path,
            raw,
            trusted_root=root,
            trusted_root_identity=root_identity,
            error="legacy session backup changed",
            max_existing_bytes=max_bytes,
        )
    _stable_private_read(
        path,
        root=root,
        root_identity=root_identity,
        max_bytes=max_bytes,
        error="legacy session backup changed",
        expected_bytes=raw,
    )


def worktree_identity(workspace_root):
    lexical_root = _lexical_absolute(workspace_root)
    root_stat = os.lstat(lexical_root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SessionFormatError("workspace root is not a directory")
    dotgit = Path(lexical_root) / ".git"
    git_dir = ""
    try:
        dotgit_stat = dotgit.lstat()
    except FileNotFoundError:
        dotgit_stat = None
    if dotgit_stat is not None and stat.S_ISDIR(dotgit_stat.st_mode):
        git_dir = _lexical_absolute(dotgit)
    elif dotgit_stat is not None and stat.S_ISREG(dotgit_stat.st_mode):
        pointer = _read_small_regular(dotgit)
        if not pointer.startswith("gitdir: "):
            raise SessionFormatError("invalid Git worktree pointer")
        raw_git_dir = pointer[len("gitdir: ") :]
        git_dir = _lexical_absolute(
            raw_git_dir if os.path.isabs(raw_git_dir) else dotgit.parent / raw_git_dir
        )
    git_common_dir = git_dir
    if git_dir:
        common_path = Path(git_dir) / "commondir"
        try:
            common = _read_small_regular(common_path)
        except FileNotFoundError:
            common = ""
        if common:
            git_common_dir = _lexical_absolute(
                common if os.path.isabs(common) else Path(git_dir) / common
            )
    facts = {
        "lexical_root": lexical_root,
        "git_common_dir": git_common_dir,
        "git_dir": git_dir,
        "root_device": int(root_stat.st_dev),
        "root_inode": int(root_stat.st_ino),
    }
    digest = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**facts, "digest": digest}


def _validate_worktree_identity(value):
    if not isinstance(value, dict) or value.keys() != _WORKTREE_FIELDS:
        raise SessionFormatError("invalid worktree identity")
    if any(
        not isinstance(value.get(key), str)
        for key in ("lexical_root", "git_common_dir", "git_dir", "digest")
    ):
        raise SessionFormatError("invalid worktree identity string")
    if any(type(value.get(key)) is not int for key in ("root_device", "root_inode")):
        raise SessionFormatError("invalid worktree identity number")
    facts = {key: value[key] for key in _WORKTREE_FIELDS if key != "digest"}
    expected = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if value["digest"] != expected:
        raise SessionFormatError("worktree identity digest mismatch")


def _validate_header(header, session_id, *, version=SESSION_FORMAT_VERSION):
    if not isinstance(header, dict) or header.keys() != _HEADER_FIELDS:
        raise SessionFormatError("invalid session header fields")
    if header.get("record_type") != SESSION_HEADER_RECORD_TYPE:
        raise SessionFormatError("invalid session header type")
    if header.get("format_version") != version:
        raise SessionFormatError("invalid session header version")
    if header.get("id") != session_id:
        raise SessionFormatError("session header id mismatch")
    if any(
        not isinstance(header.get(key), str)
        for key in ("id", "created_at", "workspace_root")
    ):
        raise SessionFormatError("invalid session header string")
    _validate_worktree_identity(header.get("worktree_identity"))
    return header


def _validate_entry(
    entry,
    known_ids,
    *,
    version=SESSION_FORMAT_VERSION,
    plan_redactor=None,
):
    if not isinstance(entry, dict) or entry.keys() != _ENTRY_FIELDS:
        raise SessionFormatError("invalid session entry fields")
    if entry.get("record_type") != SESSION_ENTRY_RECORD_TYPE:
        raise SessionFormatError("invalid session entry type")
    if entry.get("format_version") != version:
        raise SessionFormatError("invalid session entry version")
    if not isinstance(entry.get("id"), str) or not _ENTRY_ID_RE.fullmatch(entry["id"]):
        raise SessionFormatError("invalid session entry id")
    if entry["id"] in known_ids:
        raise SessionFormatError("duplicate session entry id")
    parent_id = entry.get("parent_id")
    if not isinstance(parent_id, str) or parent_id and parent_id not in known_ids:
        raise SessionFormatError("invalid session parent id")
    if not isinstance(entry.get("timestamp"), str):
        raise SessionFormatError("invalid session entry timestamp")
    if version == WORKFLOW_SESSION_FORMAT_VERSION:
        allowed_types = _V3_ENTRY_TYPES
    elif version == LEGACY_JSONL_SESSION_FORMAT_VERSION:
        allowed_types = _V2_ENTRY_TYPES
    else:
        allowed_types = ENTRY_TYPES
    if entry.get("type") not in allowed_types:
        raise SessionFormatError("invalid session entry kind")
    if not isinstance(entry.get("data"), dict):
        raise SessionFormatError("invalid session entry data")
    if entry["type"] == "compaction":
        data = entry["data"]
        required = {
            "summary",
            "first_kept_entry_id",
            "tokens_before",
            "summary_tokens",
            "tail_tokens",
            "reason",
        }
        if not required <= data.keys():
            raise SessionFormatError("invalid compaction entry data")
        if any(
            not isinstance(data.get(key), str)
            for key in ("summary", "first_kept_entry_id", "reason")
        ) or any(
            type(data.get(key)) is not int or data[key] < 0
            for key in ("tokens_before", "summary_tokens", "tail_tokens")
        ):
            raise SessionFormatError("invalid compaction entry data")
    if entry["type"] == "task_checkpoint":
        data = entry["data"]
        checkpoint_id = data.get("checkpoint_id")
        checkpoint = data.get("checkpoint")
        if (
            not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or not isinstance(checkpoint, dict)
            or checkpoint.get("checkpoint_id") != checkpoint_id
            or not checkpoint.keys()
            <= (
                _V4_TASK_CHECKPOINT_FIELDS
                if version in {PREVIOUS_SESSION_FORMAT_VERSION, WORKFLOW_SESSION_FORMAT_VERSION}
                else _TASK_CHECKPOINT_FIELDS
            )
        ):
            raise SessionFormatError("invalid task checkpoint entry data")
    if entry["type"] == "tool_exchange":
        if entry["data"].keys() != {"assistant", "result"}:
            raise SessionFormatError("invalid tool exchange entry data")
    if entry["type"] == "tool_exchange" and version == WORKFLOW_SESSION_FORMAT_VERSION:
        _tool_exchange_plan(entry["data"], redactor=plan_redactor)
    if entry["type"] == "permission_mode_change":
        keys = entry["data"].keys()
        if version not in {SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION} or keys not in (
            {"mode"},
            {"mode", "pre_mode"},
        ):
            raise SessionFormatError("invalid permission mode entry data")
        try:
            mode = validate_permission_mode(entry["data"].get("mode"))
            pre_mode = entry["data"].get("pre_mode")
            if pre_mode is not None:
                pre_mode = validate_permission_mode(pre_mode)
                if mode != PermissionMode.PLAN.value or pre_mode == mode:
                    raise ValueError("invalid pre-plan permission mode")
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from None
    if entry["type"] == "plan_artifact":
        text = entry["data"].get("text")
        revision = entry["data"].get("revision")
        if (
            version not in {SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION}
            or entry["data"].keys() != {"text", "revision"}
            or not isinstance(text, str)
            or len(text.encode("utf-8")) > MAX_PLAN_TEXT_BYTES
            or type(revision) is not int
            or revision < 1
        ):
            raise SessionFormatError("invalid plan artifact entry data")
    if entry["type"] == "workflow_mode_change":
        if version != WORKFLOW_SESSION_FORMAT_VERSION:
            raise SessionFormatError("invalid workflow mode entry data")
        if entry["data"].keys() != {"mode"}:
            raise SessionFormatError("invalid workflow mode entry data")
        try:
            validate_workflow_mode(entry["data"].get("mode"))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from None
    if entry["type"] == "plan_update":
        if version != WORKFLOW_SESSION_FORMAT_VERSION:
            raise SessionFormatError("invalid plan update entry data")
        if entry["data"].keys() != {"plan"}:
            raise SessionFormatError("invalid plan update entry data")
        try:
            validate_plan(entry["data"].get("plan"))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from None
    return entry


def _base_projection(header):
    projection = {
        "record_type": SESSION_RECORD_TYPE,
        "format_version": SESSION_FORMAT_VERSION,
        "id": header["id"],
        "created_at": header["created_at"],
        "workspace_root": header["workspace_root"],
        "messages": [],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {}},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "runtime_identity": {},
    }
    if header["format_version"] in {SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION}:
        projection.update(
            format_version=header["format_version"],
            permission_mode=PermissionMode.DEFAULT.value,
            permission_rules={"allow": [], "ask": [], "deny": []},
            plan_text="",
            plan_revision=0,
            pre_plan_mode="",
        )
        if header["format_version"] == PREVIOUS_SESSION_FORMAT_VERSION:
            projection["recovery"] = {"current_checkpoint_id": ""}
    elif header["format_version"] == WORKFLOW_SESSION_FORMAT_VERSION:
        projection.update(
            format_version=header["format_version"],
            workflow_mode=DEFAULT_WORKFLOW_MODE,
            active_plan=deepcopy(EMPTY_PLAN),
        )
    else:
        projection["format_version"] = header["format_version"]
    return projection


def _active_path(entries):
    if not entries:
        return []
    by_id = {entry["id"]: entry for entry in entries}
    path = []
    current = entries[-1]
    seen = set()
    while current is not None:
        if current["id"] in seen:
            raise SessionFormatError("session tree cycle")
        seen.add(current["id"])
        path.append(current)
        parent_id = current["parent_id"]
        current = by_id.get(parent_id) if parent_id else None
    path.reverse()
    return path


def _tool_exchange_plan(data, *, redactor=None):
    assistant = data.get("assistant")
    result = data.get("result")
    if not isinstance(assistant, dict) or not isinstance(result, dict):
        raise SessionFormatError("invalid tool exchange entry")
    blocks = assistant.get("content")
    result_blocks = result.get("content")
    metadata = result.get("_pony_meta")
    assistant_metadata = assistant.get("_pony_meta")
    if (
        not isinstance(blocks, list)
        or len(blocks) != 1
        or not isinstance(blocks[0], dict)
        or not isinstance(result_blocks, list)
        or len(result_blocks) != 1
        or not isinstance(result_blocks[0], dict)
        or not isinstance(metadata, dict)
        or not isinstance(assistant_metadata, dict)
    ):
        return None
    call = blocks[0]
    tool_result = result_blocks[0]
    tool_use_id = call.get("id")
    if (
        call.get("type") != "tool_use"
        or call.get("name") != "update_plan"
        or metadata.get("tool_status") != "ok"
        or metadata.get("effect_class") != "session_state"
        or not isinstance(tool_use_id, str)
        or assistant_metadata.get("tool_use_id") != tool_use_id
        or metadata.get("tool_use_id") != tool_use_id
        or tool_result.get("type") != "tool_result"
        or tool_result.get("tool_use_id") != tool_use_id
        or tool_result.get("is_error") is True
    ):
        return None
    arguments = call.get("input")
    if not isinstance(arguments, dict) or arguments.keys() != {"plan_json"}:
        raise SessionFormatError("invalid update_plan arguments")
    try:
        return parse_plan_json(arguments["plan_json"], redactor=redactor)
    except PlanValidationError:
        raise
    except ValueError as exc:
        raise SessionFormatError(str(exc)) from None


def _apply_entry(projection, entry, *, version):
    kind = entry["type"]
    data = entry["data"]
    if kind == "message":
        message = data.get("message")
        if not isinstance(message, dict):
            raise SessionFormatError("invalid message entry")
        projection["messages"].append(deepcopy(message))
    elif kind == "tool_exchange":
        assistant = data.get("assistant")
        result = data.get("result")
        if not isinstance(assistant, dict) or not isinstance(result, dict):
            raise SessionFormatError("invalid tool exchange entry")
        projection["messages"].extend((deepcopy(assistant), deepcopy(result)))
        if version == WORKFLOW_SESSION_FORMAT_VERSION:
            plan = _tool_exchange_plan(data)
            if plan is not None:
                projection["active_plan"] = plan
    elif kind == "permission_mode_change":
        previous = projection["permission_mode"]
        mode = validate_permission_mode(data.get("mode"))
        if mode == PermissionMode.PLAN.value and previous != mode:
            projection["pre_plan_mode"] = validate_permission_mode(
                data.get("pre_mode", previous)
            )
        elif previous == PermissionMode.PLAN.value and mode != previous:
            projection["pre_plan_mode"] = ""
        projection["permission_mode"] = mode
    elif kind == "plan_artifact":
        projection["plan_text"] = data["text"]
        projection["plan_revision"] = data["revision"]
    elif kind == "workflow_mode_change":
        projection["workflow_mode"] = validate_workflow_mode(data.get("mode"))
    elif kind == "plan_update":
        projection["active_plan"] = validate_plan(data.get("plan"))
    elif kind == "session_info":
        values = data.get("set")
        if not isinstance(values, dict) or "messages" in values:
            raise SessionFormatError("invalid session info entry")
        allowed_fields = (
            _V4_SESSION_INFO_FIELDS
            if version
            in {PREVIOUS_SESSION_FORMAT_VERSION, WORKFLOW_SESSION_FORMAT_VERSION}
            else _SESSION_INFO_FIELDS
        )
        if not values.keys() <= allowed_fields:
            raise SessionFormatError("invalid session info field")
        projection.update(
            deepcopy(
                {
                    key: value
                    for key, value in values.items()
                    if key not in _HEADER_PROJECTION_FIELDS
                }
            )
        )
    elif kind == "task_checkpoint":
        checkpoint_id = data.get("checkpoint_id")
        checkpoint = data.get("checkpoint")
        if not isinstance(checkpoint_id, str) or not isinstance(checkpoint, dict):
            raise SessionFormatError("invalid task checkpoint entry")
        checkpoints = projection.setdefault(
            "checkpoints", {"current_id": "", "items": {}}
        )
        checkpoints.setdefault("items", {})[checkpoint_id] = deepcopy(checkpoint)
        checkpoints["current_id"] = checkpoint_id
        goal = str(checkpoint.get("goal", checkpoint.get("current_goal", "")) or "")
        key_files = checkpoint.get("key_files", [])
        key_files = key_files if isinstance(key_files, list) else []
        recent_files = [
            str(item.get("path", ""))
            for item in key_files
            if isinstance(item, dict) and str(item.get("path", ""))
        ][:8]
        projection["working_memory"] = {
            "task_summary": goal[:300],
            "recent_files": recent_files,
        }
        projection["memory"] = {
            "file_summaries": {
                str(item["path"]): str(item.get("summary", "") or "")
                for item in key_files
                if isinstance(item, dict)
                and str(item.get("path", ""))
                and str(item.get("summary", "") or "").strip()
            }
        }
        projection["recently_recalled"] = []
        runtime_identity = checkpoint.get("runtime_identity")
        if isinstance(runtime_identity, dict):
            projection["runtime_identity"] = deepcopy(runtime_identity)
    return projection


def entry_message_refs(entry):
    """Return immutable-by-contract message references carried by an entry."""
    kind = entry.get("type")
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if kind == "message" and isinstance(data.get("message"), dict):
        return (data["message"],)
    if kind == "tool_exchange" and all(
        isinstance(data.get(key), dict) for key in ("assistant", "result")
    ):
        return (data["assistant"], data["result"])
    return ()


def entry_messages(entry):
    """Return defensive copies of canonical messages carried by an entry."""
    return tuple(deepcopy(message) for message in entry_message_refs(entry))


def _estimate_entry_tokens(entry):
    return sum(
        estimate_text_tokens(message_content_text(message)) + 12
        for message in entry_message_refs(entry)
    )


def _summary_message(summary, entry_id, created_at, *, kind="compaction"):
    tags = {
        "compaction": "pony:session_summary",
        "split_turn": "pony:split_turn_summary",
        "branch": "pony:branch_summary",
    }
    origins = {
        "compaction": "compaction_summary",
        "split_turn": "split_turn_summary",
        "branch": "branch_summary",
    }
    tag = tags[kind]
    safe_summary = escape_pony_tags(str(summary).strip())
    return {
        "role": "user",
        "content": (f"<{tag}>\n" + safe_summary + f"\n</{tag}>"),
        "_pony_meta": {
            "created_at": str(created_at),
            "origin": origins[kind],
            "summary_entry_id": entry_id,
        },
    }


def context_view_from_tree(tree):
    path = list(tree.active_path)
    compact_index = next(
        (
            index
            for index in range(len(path) - 1, -1, -1)
            if path[index]["type"] == "compaction"
        ),
        None,
    )
    summary = ""
    split_turn_summary = ""
    summary_tokens = 0
    split_turn_summary_tokens = 0
    tail_tokens = 0
    tokens_before = 0
    first_kept_id = ""
    compaction_id = ""
    compaction_created_at = ""
    compaction_reason = "not_compacted"
    compression_ratio = 1.0
    selected_with_indexes = []
    if compact_index is None:
        selected_with_indexes = [
            (index, entry)
            for index, entry in enumerate(path)
            if entry_message_refs(entry)
        ]
    else:
        compaction = path[compact_index]
        data = compaction["data"]
        summary = data["summary"]
        split_turn_summary = str(data.get("split_turn_summary", "") or "")
        summary_tokens = data["summary_tokens"]
        raw_split_tokens = data.get("split_turn_summary_tokens", 0)
        split_turn_summary_tokens = (
            raw_split_tokens
            if type(raw_split_tokens) is int and raw_split_tokens >= 0
            else 0
        )
        tail_tokens = data["tail_tokens"]
        tokens_before = data["tokens_before"]
        first_kept_id = data["first_kept_entry_id"]
        compaction_id = compaction["id"]
        compaction_created_at = compaction["timestamp"]
        compaction_reason = data["reason"]
        ratio = data.get("compression_ratio", 1.0)
        compression_ratio = (
            float(ratio) if type(ratio) in {int, float} and ratio >= 0 else 1.0
        )
        start = compact_index
        if first_kept_id:
            for index, entry in enumerate(path[:compact_index]):
                if entry["id"] == first_kept_id:
                    start = index
                    break
            else:
                raise SessionFormatError("compaction kept entry is not on active path")
        selected_with_indexes = [
            (index, entry)
            for index, entry in enumerate(path)
            if entry_message_refs(entry)
            and (
                (first_kept_id and start <= index < compact_index)
                or index > compact_index
            )
        ]
    selected_entries = [entry for _, entry in selected_with_indexes]
    branch_index = next(
        (
            index
            for index in range(len(path) - 1, -1, -1)
            if path[index]["type"] == "branch_summary"
            and (compact_index is None or index > compact_index)
        ),
        None,
    )
    branch_summary = ""
    branch_summary_tokens = 0
    branch_summary_entry_id = ""
    branch_created_at = ""
    if branch_index is not None:
        branch_entry = path[branch_index]
        branch_summary = str(branch_entry["data"].get("summary", "") or "")
        raw_branch_tokens = branch_entry["data"].get("summary_tokens", 0)
        branch_summary_tokens = (
            raw_branch_tokens
            if type(raw_branch_tokens) is int and raw_branch_tokens >= 0
            else 0
        )
        branch_summary_entry_id = branch_entry["id"]
        branch_created_at = branch_entry["timestamp"]

    messages = []
    if summary:
        messages.append(_summary_message(summary, compaction_id, compaction_created_at))
    if split_turn_summary:
        messages.append(
            _summary_message(
                split_turn_summary,
                compaction_id,
                compaction_created_at,
                kind="split_turn",
            )
        )
    branch_added = False
    for index, entry in selected_with_indexes:
        if branch_summary and not branch_added and index > branch_index:
            messages.append(
                _summary_message(
                    branch_summary,
                    branch_summary_entry_id,
                    branch_created_at,
                    kind="branch",
                )
            )
            branch_added = True
        messages.extend(entry_messages(entry))
    if branch_summary and not branch_added:
        messages.append(
            _summary_message(
                branch_summary,
                branch_summary_entry_id,
                branch_created_at,
                kind="branch",
            )
        )
    return SessionContextView(
        messages=tuple(messages),
        message_entries=tuple(deepcopy(selected_entries)),
        summary=summary,
        split_turn_summary=split_turn_summary,
        branch_summary=branch_summary,
        summary_tokens=summary_tokens,
        split_turn_summary_tokens=split_turn_summary_tokens,
        branch_summary_tokens=branch_summary_tokens,
        tail_tokens=tail_tokens,
        tokens_before=tokens_before,
        first_kept_entry_id=first_kept_id,
        compaction_entry_id=compaction_id,
        compaction_reason=compaction_reason,
        compression_ratio=compression_ratio,
        branch_summary_entry_id=branch_summary_entry_id,
        canonical_message_count=len(tree.projection["messages"]),
        canonical_last_message=(
            deepcopy(tree.projection["messages"][-1])
            if tree.projection["messages"]
            else None
        ),
    )


def _project_tree(header, entries):
    projection = _base_projection(header)
    path = _active_path(entries)
    for entry in path:
        _apply_entry(projection, entry, version=header["format_version"])
    _validate_projection(
        projection,
        header["id"],
        version=header["format_version"],
    )
    return path, projection


def _new_entry(kind, data, parent_id):
    return {
        "record_type": SESSION_ENTRY_RECORD_TYPE,
        "format_version": SESSION_FORMAT_VERSION,
        "id": uuid.uuid4().hex[:24],
        "parent_id": str(parent_id or ""),
        "timestamp": now(),
        "type": kind,
        "data": deepcopy(data),
    }


def _message_entries(messages, parent_id):
    entries = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if (
            message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
        ):
            if index + 1 >= len(messages):
                raise SessionFormatError("tool call is missing its result")
            entry = _new_entry(
                "tool_exchange",
                {"assistant": message, "result": messages[index + 1]},
                parent_id,
            )
            index += 2
        else:
            entry = _new_entry("message", {"message": message}, parent_id)
            index += 1
        entries.append(entry)
        parent_id = entry["id"]
    return entries


def _state_values(projection):
    return {
        key: deepcopy(value)
        for key, value in projection.items()
        if key in _SESSION_INFO_FIELDS
    }


def _permission_entries(projection, parent_id):
    mode = projection.get("permission_mode", PermissionMode.DEFAULT.value)
    if mode == PermissionMode.DEFAULT.value:
        return []
    data = {"mode": mode}
    pre_mode = str(projection.get("pre_plan_mode", "") or "")
    if mode == PermissionMode.PLAN.value and pre_mode:
        data["pre_mode"] = pre_mode
    return [_new_entry("permission_mode_change", data, parent_id)]


def _plan_entries(projection, parent_id):
    text = str(projection.get("plan_text", "") or "")
    revision = int(projection.get("plan_revision", 0) or 0)
    if not text and revision == 0:
        return []
    return [
        _new_entry(
            "plan_artifact",
            {"text": text, "revision": revision},
            parent_id,
        )
    ]


def _persistent_projection(value):
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in _DERIVED_CACHE_FIELDS
    }


def session_projections_equal(left, right):
    """Compare canonical Session state while ignoring rebuildable caches."""
    return _persistent_projection(left) == _persistent_projection(right)


def _ordered_checkpoint_items(checkpoints):
    if not isinstance(checkpoints, dict):
        return []
    items = checkpoints.get("items")
    if not isinstance(items, dict):
        return []
    current_id = str(checkpoints.get("current_id", "") or "")
    ordered = [
        (str(checkpoint_id), checkpoint)
        for checkpoint_id, checkpoint in items.items()
        if str(checkpoint_id) != current_id
    ]
    if current_id and current_id in items:
        ordered.append((current_id, items[current_id]))
    return ordered


def _task_checkpoint_entries(checkpoints, parent_id, *, known_ids=()):
    entries = []
    known = set(known_ids)
    for checkpoint_id, checkpoint in _ordered_checkpoint_items(checkpoints):
        entry = _new_entry(
            "task_checkpoint",
            {
                "checkpoint_id": checkpoint_id,
                "checkpoint": checkpoint,
            },
            parent_id,
        )
        _validate_entry(entry, known)
        entries.append(entry)
        known.add(entry["id"])
        parent_id = entry["id"]
    return entries


def _promote_legacy_working_state(session, worktree_digest):
    """Turn legacy mutable working state into one deterministic checkpoint."""
    working = session.get("working_memory")
    working = working if isinstance(working, dict) else {}
    goal = str(working.get("task_summary", "") or "").strip()
    recent_files = working.get("recent_files")
    recent_files = recent_files if isinstance(recent_files, list) else []
    recent_files = list(
        dict.fromkeys(str(path).strip() for path in recent_files if str(path).strip())
    )[:8]
    if not goal and not recent_files:
        return

    memory = session.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    summaries = memory.get("file_summaries")
    summaries = summaries if isinstance(summaries, dict) else {}
    key_files = []
    for path in recent_files:
        raw_summary = summaries.get(path, "")
        summary = (
            raw_summary.get("summary", "")
            if isinstance(raw_summary, dict)
            else raw_summary
        )
        key_files.append(
            {
                "path": path,
                "freshness": {},
                "summary": str(summary or "").strip(),
            }
        )

    checkpoints = session.get("checkpoints")
    checkpoints = deepcopy(checkpoints) if isinstance(checkpoints, dict) else {}
    items = checkpoints.get("items")
    items = deepcopy(items) if isinstance(items, dict) else {}
    parent_id = str(checkpoints.get("current_id", "") or "")
    seed = json.dumps(
        {
            "session_id": session.get("id", ""),
            "created_at": session.get("created_at", ""),
            "goal": goal,
            "recent_files": recent_files,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    checkpoint_id = (
        "ckpt_migrated_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    )
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": parent_id,
        "created_at": str(session.get("created_at", "") or now()),
        "goal": goal,
        "status": "in_progress",
        "completed": [],
        "in_progress": [goal] if goal else [],
        "blocker": "",
        "next_steps": [],
        "key_files": key_files,
        "read_files": recent_files,
        "modified_files": [],
        "worktree_identity_digest": str(worktree_digest),
        "context_usage": {},
        "label": "legacy working state",
        "trigger": "legacy_migration",
        "summary": "legacy working state migration",
        "runtime_identity": deepcopy(session.get("runtime_identity", {})),
    }
    items[checkpoint_id] = checkpoint
    session["checkpoints"] = {
        "current_id": checkpoint_id,
        "items": items,
    }


def _serialize_line(value):
    rendered = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(rendered) > MAX_SESSION_ENTRY_BYTES:
        raise ValueError("session entry too large")
    return rendered


def _render_tree(header, entries):
    return b"".join(
        [_serialize_line(header), *(_serialize_line(item) for item in entries)]
    )


def _jsonl_format_version(raw):
    first = raw.split(b"\n", 1)[0]
    header = _decode_json_object(first)
    version = header.get("format_version")
    if type(version) is not int:
        raise SessionFormatError("invalid session header version")
    return version


def _parse_jsonl(raw, session_id, *, version=SESSION_FORMAT_VERSION):
    if not raw:
        raise SessionFormatError("empty session tree")
    if not raw.endswith(b"\n"):
        valid_bytes = raw.rfind(b"\n") + 1
        raise SessionTailRepairRequired(session_id, valid_bytes)
    raw_lines = raw.splitlines()
    if not raw_lines:
        raise SessionFormatError("empty session tree")
    for line in raw_lines:
        if len(line) + 1 > MAX_SESSION_ENTRY_BYTES:
            raise SessionFormatError("session entry too large")
    header = _validate_header(
        _decode_json_object(raw_lines[0]),
        session_id,
        version=version,
    )
    if worktree_identity(header["workspace_root"]) != header["worktree_identity"]:
        raise SessionFormatError("session worktree identity mismatch")
    entries = []
    known_ids = set()
    for line in raw_lines[1:]:
        entry = _validate_entry(
            _decode_json_object(line),
            known_ids,
            version=version,
        )
        known_ids.add(entry["id"])
        entries.append(entry)
    path, projection = _project_tree(header, entries)
    return SessionTree(
        header,
        tuple(entries),
        tuple(path),
        projection,
        {entry["id"]: _estimate_entry_tokens(entry) for entry in entries},
    )


def _restore_expected_projection(header, entries, expected):
    """Append one state delta when control entries intentionally project history.

    A checkpoint carries the runtime state that was true when it was created.
    When importing a full projection, that historical state must not replace
    the caller's current state. Keep the checkpoint as a first-class entry,
    then restore only the fields whose projected values differ.
    """
    tree = _parse_jsonl(_render_tree(header, entries), header["id"])
    if tree.projection["messages"] != expected["messages"]:
        raise SessionFormatError("session message projection mismatch")
    changed = {
        key: deepcopy(expected[key])
        for key in _SESSION_INFO_FIELDS & expected.keys()
        if tree.projection.get(key) != expected[key]
    }
    if changed:
        parent_id = entries[-1]["id"] if entries else ""
        entries.append(_new_entry("session_info", {"set": changed}, parent_id))
    projected = _parse_jsonl(_render_tree(header, entries), header["id"]).projection
    parent_id = entries[-1]["id"] if entries else ""
    if projected["permission_mode"] != expected["permission_mode"]:
        data = {"mode": expected["permission_mode"]}
        pre_mode = str(expected.get("pre_plan_mode", "") or "")
        if expected["permission_mode"] == PermissionMode.PLAN.value and pre_mode:
            data["pre_mode"] = pre_mode
        entry = _new_entry(
            "permission_mode_change",
            data,
            parent_id,
        )
        entries.append(entry)
        parent_id = entry["id"]
    if (
        projected.get("plan_text", "") != expected.get("plan_text", "")
        or projected.get("plan_revision", 0) != expected.get("plan_revision", 0)
    ):
        entries.extend(_plan_entries(expected, parent_id))
    return entries


def _extend_tree(tree, entries):
    entries = tuple(entries)
    if not entries:
        return tree
    is_linear_append = entries[0]["parent_id"] == tree.leaf_id and all(
        entry["parent_id"] == entries[index - 1]["id"]
        for index, entry in enumerate(entries[1:], start=1)
    )
    combined = tree.entries + entries
    if not is_linear_append:
        path, projection = _project_tree(tree.header, combined)
        estimates = dict(tree.entry_token_estimates)
        estimates.update(
            {entry["id"]: _estimate_entry_tokens(entry) for entry in entries}
        )
        return SessionTree(
            tree.header,
            combined,
            tuple(path),
            projection,
            estimates,
        )
    projection = dict(tree.projection)
    messages_copied = False
    checkpoints_copied = False
    for entry in entries:
        if entry["type"] in {"message", "tool_exchange"} and not messages_copied:
            projection["messages"] = list(tree.projection["messages"])
            messages_copied = True
        if entry["type"] == "task_checkpoint" and not checkpoints_copied:
            checkpoints = tree.projection.get("checkpoints", {})
            checkpoints = dict(checkpoints) if isinstance(checkpoints, dict) else {}
            items = checkpoints.get("items", {})
            checkpoints["items"] = dict(items) if isinstance(items, dict) else {}
            projection["checkpoints"] = checkpoints
            checkpoints_copied = True
        _apply_entry(projection, entry, version=tree.header["format_version"])
    return SessionTree(
        tree.header,
        combined,
        tree.active_path + entries,
        projection,
        {
            **tree.entry_token_estimates,
            **{entry["id"]: _estimate_entry_tokens(entry) for entry in entries},
        },
    )


def _carry_provider_binding_across_branch(tree, entries):
    current = tree.projection.get("provider_binding")
    if not isinstance(current, dict):
        return list(entries)
    branched = _extend_tree(tree, entries)
    if branched.projection.get("provider_binding") == current:
        return list(entries)
    return [
        *entries,
        _new_entry(
            "session_info",
            {"set": {"provider_binding": deepcopy(current)}},
            entries[-1]["id"],
        ),
    ]


class SessionStore:
    def __init__(self, root, redactor=None):
        self.root = harden_private_tree(root)
        self._root_identity = private_directory_identity(self.root)
        self.lock_path = self.root / ".session_store.lock"
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None
        self._soft_limit_warned = set()
        self._tree_cache = {}

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None

    @staticmethod
    def projections_equal(left, right):
        return session_projections_equal(left, right)

    def path(self, session_id):
        return self.root / f"{_session_id(session_id)}.jsonl"

    def legacy_path(self, session_id):
        return self.root / f"{_session_id(session_id)}.json"

    def candidate_path(self, session_id):
        return self.root / f"{_session_id(session_id)}.jsonl.candidate"

    def path_for(self, session_id):
        return self.path(session_id)

    def _read_tree_unlocked(self, session_id):
        path = self.path(session_id)
        signature = private_file_signature(
            path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        cached = self._tree_cache.get(session_id)
        if cached is not None and cached[0] == signature:
            return cached[1]
        raw = read_private_bytes(
            path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
        )
        version = _jsonl_format_version(raw)
        if version != SESSION_FORMAT_VERSION:
            if version in {
                LEGACY_JSONL_SESSION_FORMAT_VERSION,
                PREVIOUS_SESSION_FORMAT_VERSION,
                WORKFLOW_SESSION_FORMAT_VERSION,
            }:
                raise SessionMigrationRequired(session_id)
            raise SessionFormatError("invalid session header version")
        tree = _parse_jsonl(raw, session_id)
        final_signature = private_file_signature(
            path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        if signature[:5] != final_signature[:5]:
            raise SessionFormatError("session tree changed while reading")
        self._tree_cache[session_id] = (final_signature, tree)
        return tree

    def load_tree(self, session_id, *, migrate=False):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            if not self.path(session_id).exists():
                if self.legacy_path(session_id).exists():
                    raise SessionMigrationRequired(session_id)
                raise FileNotFoundError(self.path(session_id))
            return self._read_tree_unlocked(session_id)

    def load(self, session_id):
        return deepcopy(self.load_tree(session_id).projection)

    def load_for_resume(self, session_id):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            if self.path(session_id).exists():
                raw = read_private_bytes(
                    self.path(session_id),
                    trusted_root=self.root,
                    trusted_root_identity=self._root_identity,
                    max_bytes=MAX_SESSION_BYTES,
                )
                version = _jsonl_format_version(raw)
                if version in {
                    LEGACY_JSONL_SESSION_FORMAT_VERSION,
                    PREVIOUS_SESSION_FORMAT_VERSION,
                    WORKFLOW_SESSION_FORMAT_VERSION,
                }:
                    self._migrate_jsonl_unlocked(session_id, version, raw)
                elif version != SESSION_FORMAT_VERSION:
                    raise SessionFormatError("invalid session header version")
            else:
                self._migrate_legacy_unlocked(session_id)
            return deepcopy(self._read_tree_unlocked(session_id).projection)

    def _load_unlocked(self, session_id):
        session_id = _session_id(session_id)
        if not self.path(session_id).exists():
            if self.legacy_path(session_id).exists():
                raise SessionMigrationRequired(session_id)
            raise FileNotFoundError(self.path(session_id))
        return deepcopy(self._read_tree_unlocked(session_id).projection)

    def _render_new_tree(self, session, *, migration=False):
        session_id = session["id"]
        header = {
            "record_type": SESSION_HEADER_RECORD_TYPE,
            "format_version": SESSION_FORMAT_VERSION,
            "id": session_id,
            "created_at": session["created_at"],
            "workspace_root": session["workspace_root"],
            "worktree_identity": worktree_identity(session["workspace_root"]),
        }
        initial_state = deepcopy(session)
        if _ordered_checkpoint_items(session.get("checkpoints", {})):
            initial_state["checkpoints"] = {"current_id": "", "items": {}}
        first = _new_entry(
            "session_info",
            {"set": _state_values(initial_state)},
            "",
        )
        entries = [first]
        parent_id = first["id"]
        if migration:
            marker = _new_entry(
                "migration",
                {"from_format": LEGACY_SESSION_FORMAT_VERSION},
                parent_id,
            )
            entries.append(marker)
            parent_id = marker["id"]
        permission_entries = _permission_entries(session, parent_id)
        entries.extend(permission_entries)
        if permission_entries:
            parent_id = permission_entries[-1]["id"]
        plan_entries = _plan_entries(session, parent_id)
        entries.extend(plan_entries)
        if plan_entries:
            parent_id = plan_entries[-1]["id"]
        checkpoint_entries = _task_checkpoint_entries(
            session.get("checkpoints", {}),
            parent_id,
            known_ids={entry["id"] for entry in entries},
        )
        entries.extend(checkpoint_entries)
        if checkpoint_entries:
            parent_id = checkpoint_entries[-1]["id"]
        entries.extend(_message_entries(session["messages"], parent_id))
        entries = _restore_expected_projection(header, entries, session)
        rendered = _render_tree(header, entries)
        if len(rendered) > MAX_SESSION_BYTES:
            raise ValueError("private file too large")
        tree = _parse_jsonl(rendered, session_id)
        if not session_projections_equal(tree.projection, session):
            raise SessionFormatError("session tree projection mismatch")
        return rendered, tree

    def _write_new_tree_unlocked(self, session, *, migration=False):
        session_id = session["id"]
        rendered, tree = self._render_new_tree(session, migration=migration)
        write_private_bytes_atomic(
            self.path(session_id),
            rendered,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            error="session tree changed",
            max_existing_bytes=MAX_SESSION_BYTES,
        )
        signature = private_file_signature(
            self.path(session_id),
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        self._tree_cache[session_id] = (signature, tree)
        return self.path(session_id)

    def _append_entries_unlocked(self, session_id, entries):
        if not entries:
            return self.path(session_id)
        tree = self._read_tree_unlocked(session_id)
        known = {entry["id"] for entry in tree.entries}
        for entry in entries:
            _validate_entry(entry, known, plan_redactor=self._redactor)
            known.add(entry["id"])
        extended = _extend_tree(tree, entries)
        _validate_projection(extended.projection, session_id)
        rendered = b"".join(_serialize_line(entry) for entry in entries)
        cached = self._tree_cache.get(session_id)
        expected_identity = cached[0][:2] if cached is not None else None
        path = append_private_bytes(
            self.path(session_id),
            rendered,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_total_bytes=MAX_SESSION_BYTES,
            expected_identity=expected_identity,
        )
        signature = private_file_signature(
            path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        self._tree_cache[session_id] = (signature, extended)
        size = path.stat().st_size
        if (
            size >= SESSION_SOFT_LIMIT_BYTES
            and session_id not in self._soft_limit_warned
        ):
            warnings.warn(
                f"session {session_id} exceeds 128 MiB; compact or clone it",
                RuntimeWarning,
                stacklevel=2,
            )
            self._soft_limit_warned.add(session_id)
        return path

    def save(self, session, *, force_branch=False, expected_leaf_id=None):
        if not isinstance(session, dict):
            raise SessionFormatError("session payload must be an object")
        raw_session = deepcopy(session)
        raw_messages = raw_session.get("messages")
        if isinstance(raw_messages, list):
            try:
                validate_messages(raw_messages, require_meta=True)
            except MessageValidationError as exc:
                raise SessionFormatError(str(exc)) from None
            known = set()
            for entry in _message_entries(raw_messages, ""):
                _validate_entry(entry, known, plan_redactor=self._redactor)
                known.add(entry["id"])
        candidate = self._redactor(deepcopy(session))
        candidate.pop("_recall_errors", None)
        session_id = _session_id(candidate.get("id"))
        candidate["format_version"] = SESSION_FORMAT_VERSION
        candidate.setdefault("permission_mode", PermissionMode.AUTO.value)
        candidate.setdefault(
            "permission_rules",
            {"allow": [], "ask": [], "deny": []},
        )
        candidate.setdefault("plan_text", "")
        candidate.setdefault("plan_revision", 0)
        candidate.setdefault("pre_plan_mode", "")
        _validate_projection(candidate, session_id)
        with file_lock.locked_file(self.lock_path):
            canonical = self.path(session_id)
            if not canonical.exists():
                if self.legacy_path(session_id).exists():
                    raise SessionMigrationRequired(session_id)
                return self._write_new_tree_unlocked(candidate)
            tree = self._read_tree_unlocked(session_id)
            if expected_leaf_id is not None and tree.leaf_id != expected_leaf_id:
                raise SessionFormatError("session changed before branch write")
            if tree.header["workspace_root"] != candidate["workspace_root"]:
                raise SessionFormatError("session workspace root changed")
            current = tree.projection
            if current.get("provider_binding") != candidate.get("provider_binding"):
                raise SessionFormatError("session provider binding changed")
            parent_id = tree.leaf_id
            entries = []
            current_messages = current["messages"]
            candidate_messages = candidate["messages"]
            if (
                not force_branch
                and candidate_messages[: len(current_messages)] == current_messages
            ):
                if any(
                    candidate.get(key) != current.get(key)
                    for key in (
                        "permission_mode",
                        "permission_rules",
                        "plan_text",
                        "plan_revision",
                        "pre_plan_mode",
                    )
                ):
                    raise SessionFormatError(
                        "permission state requires an explicit control entry"
                    )
                message_entries = _message_entries(
                    candidate_messages[len(current_messages) :],
                    parent_id,
                )
                entries.extend(message_entries)
                if message_entries:
                    parent_id = message_entries[-1]["id"]
                current_checkpoints = current.get("checkpoints", {})
                candidate_checkpoints = candidate.get("checkpoints", {})
                current_items = current_checkpoints.get("items", {})
                candidate_items = candidate_checkpoints.get("items", {})
                if not isinstance(current_items, dict) or not isinstance(
                    candidate_items,
                    dict,
                ):
                    raise SessionFormatError("invalid checkpoint state")
                if any(
                    checkpoint_id not in candidate_items
                    for checkpoint_id in current_items
                ):
                    raise SessionFormatError(
                        "checkpoint history cannot be deleted without a session branch"
                    )
                checkpoint_ids = [
                    checkpoint_id
                    for checkpoint_id, checkpoint in candidate_items.items()
                    if current_items.get(checkpoint_id) != checkpoint
                ]
                candidate_current_id = str(
                    candidate_checkpoints.get("current_id", "") or ""
                )
                current_current_id = str(
                    current_checkpoints.get("current_id", "") or ""
                )
                if (
                    candidate_current_id
                    and candidate_current_id != current_current_id
                    and candidate_current_id not in checkpoint_ids
                ):
                    checkpoint_ids.append(candidate_current_id)
                if checkpoint_ids:
                    selected = {
                        checkpoint_id: candidate_items[checkpoint_id]
                        for checkpoint_id in checkpoint_ids
                    }
                    checkpoint_state = {
                        "current_id": candidate_current_id,
                        "items": selected,
                    }
                    checkpoint_entries = _task_checkpoint_entries(
                        checkpoint_state,
                        parent_id,
                        known_ids={entry["id"] for entry in tree.entries}
                        | {entry["id"] for entry in entries},
                    )
                    entries.extend(checkpoint_entries)
                    if checkpoint_entries:
                        parent_id = checkpoint_entries[-1]["id"]
                # A task checkpoint projects its historical runtime identity onto
                # the active branch. Restore the caller's current state after
                # encoding the checkpoint entries.
                projected = _extend_tree(tree, entries).projection
                changed = {
                    key: deepcopy(candidate[key])
                    for key in _REQUIRED_FIELDS
                    - {"messages", "checkpoints"}
                    - _DERIVED_CACHE_FIELDS
                    if candidate[key] != projected[key]
                }
            else:
                rewind = _new_entry(
                    "rewind",
                    {"target_entry_id": "", "reason": "session_projection_rewrite"},
                    "",
                )
                entries.append(rewind)
                parent_id = rewind["id"]
                state = _new_entry(
                    "session_info",
                    {"set": _state_values(candidate)},
                    parent_id,
                )
                entries.append(state)
                parent_id = state["id"]
                permission_entries = _permission_entries(candidate, parent_id)
                entries.extend(permission_entries)
                if permission_entries:
                    parent_id = permission_entries[-1]["id"]
                plan_entries = _plan_entries(candidate, parent_id)
                entries.extend(plan_entries)
                if plan_entries:
                    parent_id = plan_entries[-1]["id"]
                message_entries = _message_entries(candidate_messages, parent_id)
                entries.extend(message_entries)
                changed = {}
                if message_entries:
                    parent_id = message_entries[-1]["id"]
            if changed:
                info = _new_entry("session_info", {"set": changed}, parent_id)
                entries.append(info)
            projected = _extend_tree(tree, entries).projection
            if _persistent_projection(projected) != _persistent_projection(candidate):
                raise SessionFormatError("session tree projection mismatch")
            return self._append_entries_unlocked(session_id, entries)

    def append_messages(self, session_id, messages, *, state_updates=None):
        """Atomically append one message batch without rescanning full history.

        A tool call/result pair is encoded as one ``tool_exchange`` entry.  Only
        the new batch and the small canonical runtime state are validated here;
        the already validated cached Session Tree remains immutable.
        """
        session_id = _session_id(session_id)
        raw_messages = deepcopy(list(messages or ()))
        try:
            validate_messages(raw_messages, require_meta=True)
        except MessageValidationError as exc:
            raise SessionFormatError(str(exc)) from None
        raw_known = set()
        for entry in _message_entries(raw_messages, ""):
            _validate_entry(entry, raw_known, plan_redactor=self._redactor)
            raw_known.add(entry["id"])
        safe_messages = self._redactor(raw_messages)
        if not safe_messages and not state_updates:
            return self.path(session_id)
        safe_state = self._redactor(deepcopy(state_updates or {}))
        _validate_session_info_updates(safe_state)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            parent_id = tree.leaf_id
            entries = _message_entries(safe_messages, parent_id)
            known = {entry["id"] for entry in tree.entries}
            for entry in entries:
                _validate_entry(entry, known)
                known.add(entry["id"])
            if entries:
                parent_id = entries[-1]["id"]
            changed = {
                key: value
                for key, value in safe_state.items()
                if value != tree.projection.get(key)
            }
            if changed:
                entries.append(_new_entry("session_info", {"set": changed}, parent_id))
            return self._append_entries_unlocked(session_id, entries)

    def set_provider_model(self, session_id, binding, *, expected_binding):
        """Atomically replace only the model in the active Provider binding."""
        session_id = _session_id(session_id)
        candidate = self._redactor(deepcopy(binding))
        expected = deepcopy(expected_binding)
        _validate_provider_binding(candidate)
        _validate_provider_binding(expected)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            current = tree.projection.get("provider_binding")
            if current != expected:
                raise SessionFormatError("model_session_mismatch")
            if (
                candidate["protocol_family"] != current["protocol_family"]
                or candidate["endpoint_hash"] != current["endpoint_hash"]
            ):
                raise SessionFormatError("model_session_mismatch")
            if candidate == current:
                return None
            entry = _new_entry(
                "session_info",
                {"set": {"provider_binding": candidate}},
                tree.leaf_id,
            )
            self._append_entries_unlocked(session_id, [entry])
            return deepcopy(entry)

    def append_control(
        self,
        session_id,
        kind,
        data,
        *,
        parent_id=None,
        expected_leaf_id=None,
    ):
        if kind not in ENTRY_TYPES - {
            "message",
            "tool_exchange",
            "session_info",
            "plan_artifact",
        }:
            raise ValueError("invalid control entry type")
        session_id = _session_id(session_id)
        if kind == "permission_mode_change":
            if not isinstance(data, dict) or data.keys() != {"mode"}:
                raise SessionFormatError("invalid permission mode entry data")
            validate_permission_mode(data["mode"])
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            if expected_leaf_id is not None and tree.leaf_id != expected_leaf_id:
                raise SessionFormatError("session changed before control append")
            target = tree.leaf_id if parent_id is None else str(parent_id)
            known = {entry["id"] for entry in tree.entries}
            if target and target not in known:
                raise SessionFormatError("unknown parent entry")
            safe_data = self._redactor(deepcopy(data))
            if not isinstance(safe_data, dict):
                raise SessionFormatError("control entry data must be an object")
            entry = _new_entry(kind, safe_data, target)
            _validate_entry(entry, known)
            entries = [entry]
            if kind == "rewind":
                entries = _carry_provider_binding_across_branch(tree, entries)
            self._append_entries_unlocked(session_id, entries)
            return deepcopy(entry)

    def set_permission_mode(self, session_id, mode, *, pre_mode=None):
        return self.update_permissions(
            session_id,
            mode=mode,
            pre_mode=pre_mode,
        )["mode_entry"]

    def set_permission_rule(self, session_id, tool_name, behavior):
        result = self.update_permissions(
            session_id,
            rule_updates=((tool_name, behavior),),
        )
        return result["rules"]

    def set_permission_rules(self, session_id, updates):
        return self.update_permissions(session_id, rule_updates=updates)["rules"]

    def update_permissions(
        self,
        session_id,
        *,
        mode=None,
        pre_mode=None,
        rule_updates=(),
    ):
        session_id = _session_id(session_id)
        if mode is not None:
            mode = validate_permission_mode(mode)
        if pre_mode is not None:
            pre_mode = validate_permission_mode(pre_mode)
            if mode != PermissionMode.PLAN.value or pre_mode == mode:
                raise ValueError("invalid pre-plan permission mode")
        rule_updates = tuple(
            (str(tool_name).strip(), str(behavior))
            for tool_name, behavior in rule_updates
        )
        if any(not tool_name for tool_name, _behavior in rule_updates):
            raise ValueError("permission rule tool is required")
        if any(
            behavior not in {"allow", "ask", "deny", "remove"}
            for _tool_name, behavior in rule_updates
        ):
            raise ValueError("invalid permission rule behavior")
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            entries = []
            parent_id = tree.leaf_id
            mode_entry = None
            if mode is not None and tree.projection["permission_mode"] != mode:
                data = {"mode": mode}
                if pre_mode is not None:
                    data["pre_mode"] = pre_mode
                mode_entry = _new_entry("permission_mode_change", data, parent_id)
                entries.append(mode_entry)
                parent_id = mode_entry["id"]
            rules = deepcopy(
                tree.projection.get(
                    "permission_rules",
                    {"allow": [], "ask": [], "deny": []},
                )
            )
            for tool_name, behavior in rule_updates:
                for values in rules.values():
                    if tool_name in values:
                        values.remove(tool_name)
                if behavior != "remove":
                    rules[behavior].append(tool_name)
            for values in rules.values():
                values.sort()
            safe_rules = None
            if rules != tree.projection.get("permission_rules"):
                safe_rules = self._redactor(rules)
                _validate_session_info_updates({"permission_rules": safe_rules})
                entries.append(
                    _new_entry(
                        "session_info",
                        {"set": {"permission_rules": safe_rules}},
                        parent_id,
                    )
                )
            if entries:
                self._append_entries_unlocked(session_id, entries)
            return {
                "mode_entry": deepcopy(mode_entry),
                "rules": deepcopy(safe_rules),
            }

    def set_plan_text(self, session_id, text, *, expected_revision=None):
        session_id = _session_id(session_id)
        if not isinstance(text, str):
            raise ValueError("plan text must be a string")
        if len(text.encode("utf-8")) > MAX_PLAN_TEXT_BYTES:
            raise ValueError("plan text exceeds limit")
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            if (
                expected_revision is not None
                and tree.projection.get("plan_revision", 0) != expected_revision
            ):
                raise PlanApprovalChanged("plan changed while editing")
            if tree.projection.get("plan_text", "") == text:
                return None
            revision = int(tree.projection.get("plan_revision", 0)) + 1
            safe_data = self._redactor({"text": text, "revision": revision})
            entry = _new_entry("plan_artifact", safe_data, tree.leaf_id)
            self._append_entries_unlocked(session_id, [entry])
            return deepcopy(entry)

    def exit_plan_mode(
        self,
        session_id,
        *,
        plan_text,
        plan_revision,
        expected_leaf_id,
    ):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            projection = tree.projection
            if (
                tree.leaf_id != expected_leaf_id
                or projection.get("permission_mode") != PermissionMode.PLAN.value
                or projection.get("plan_text", "") != plan_text
                or projection.get("plan_revision", 0) != plan_revision
            ):
                raise PlanApprovalChanged("plan changed during approval")
            target = projection.get("pre_plan_mode") or PermissionMode.AUTO.value
            entry = _new_entry(
                "permission_mode_change",
                {"mode": validate_permission_mode(target)},
                tree.leaf_id,
            )
            self._append_entries_unlocked(session_id, [entry])
            return deepcopy(entry)

    def append_task_checkpoint(self, session_id, checkpoint, *, parent_id=None):
        checkpoint = deepcopy(checkpoint)
        checkpoint_id = str(checkpoint.get("checkpoint_id", "") or "")
        if not checkpoint_id:
            raise ValueError("task checkpoint id is required")
        return self.append_control(
            session_id,
            "task_checkpoint",
            {"checkpoint_id": checkpoint_id, "checkpoint": checkpoint},
            parent_id=parent_id,
        )

    def rewind(
        self,
        session_id,
        entry_id,
        *,
        summary="",
        target_checkpoint_id="",
        expected_leaf_id=None,
    ):
        data = {
            "target_entry_id": str(entry_id or ""),
            "summary": str(summary or ""),
            "target_checkpoint_id": str(target_checkpoint_id or ""),
        }
        return self.append_control(
            session_id,
            "rewind",
            data,
            parent_id=str(entry_id or ""),
            expected_leaf_id=expected_leaf_id,
        )

    def rewind_with_summary(
        self,
        session_id,
        entry_id,
        summary_data,
        *,
        target_checkpoint_id="",
        expected_leaf_id,
    ):
        session_id = _session_id(session_id)
        if not isinstance(summary_data, dict):
            raise SessionFormatError("branch summary data must be an object")
        rewind_data = {
            "target_entry_id": str(entry_id or ""),
            "summary": str(summary_data.get("summary", "") or ""),
            "target_checkpoint_id": str(target_checkpoint_id or ""),
        }
        rewind_data = self._redactor(rewind_data)
        safe_summary = self._redactor(deepcopy(summary_data))
        if not isinstance(safe_summary, dict):
            raise SessionFormatError("branch summary data must be an object")
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            if tree.leaf_id != expected_leaf_id:
                raise SessionFormatError("session changed before rewind")
            target = str(entry_id or "")
            known = {entry["id"] for entry in tree.entries}
            if target not in known:
                raise SessionFormatError("unknown parent entry")
            rewind_entry = _new_entry("rewind", rewind_data, target)
            _validate_entry(rewind_entry, known)
            summary_entry = _new_entry(
                "branch_summary",
                safe_summary,
                rewind_entry["id"],
            )
            _validate_entry(summary_entry, known | {rewind_entry["id"]})
            entries = _carry_provider_binding_across_branch(
                tree,
                [rewind_entry, summary_entry],
            )
            self._append_entries_unlocked(
                session_id,
                entries,
            )
            return deepcopy(rewind_entry), deepcopy(summary_entry)

    def fork(self, session_id, entry_id):
        return self.append_control(
            session_id,
            "rewind",
            {"target_entry_id": str(entry_id or ""), "reason": "fork"},
            parent_id=str(entry_id or ""),
        )

    def label(self, session_id, label, *, entry_id=None):
        return self.append_control(
            session_id,
            "label",
            {"label": str(label)},
            parent_id=entry_id,
        )

    def clone_to_worktree(
        self,
        session_id,
        target_workspace_root,
        *,
        new_session_id=None,
    ):
        """Clone the active branch without carrying workspace-specific state."""
        tree = self.load_tree(session_id)
        target_root = Path(_lexical_absolute(target_workspace_root))
        if not target_root.is_dir():
            raise ValueError("clone target worktree does not exist")
        if _lexical_absolute(tree.header["workspace_root"]) == str(target_root):
            raise ValueError("clone target must be a different worktree")
        clone_id = _session_id(
            new_session_id or f"{session_id}-clone-{uuid.uuid4().hex[:8]}"
        )
        projection = deepcopy(tree.projection)
        projection.update(
            {
                "id": clone_id,
                "created_at": now(),
                "workspace_root": str(target_root),
                "recently_recalled": [],
                "checkpoints": {"current_id": "", "items": {}},
                "resume_state": {},
                "runtime_identity": {},
                "memory": {"file_summaries": {}},
                "working_memory": {"task_summary": "", "recent_files": []},
            }
        )
        checkpoint_state = tree.projection.get("checkpoints", {})
        checkpoint_items = (
            checkpoint_state.get("items", {})
            if isinstance(checkpoint_state, dict)
            else {}
        )
        source_checkpoint_id = (
            str(checkpoint_state.get("current_id", "") or "")
            if isinstance(checkpoint_state, dict)
            else ""
        )
        source_checkpoint = (
            deepcopy(checkpoint_items.get(source_checkpoint_id))
            if isinstance(checkpoint_items, dict)
            and isinstance(checkpoint_items.get(source_checkpoint_id), dict)
            else None
        )

        target_store = SessionStore(
            target_root / ".pony" / "sessions",
            redactor=self._redactor,
        )
        projection = target_store._redactor(projection)
        _rendered, target_tree = target_store._render_new_tree(projection)
        source_message_entries = [
            entry for entry in tree.active_path if entry_messages(entry)
        ]
        target_message_entries = [
            entry for entry in target_tree.active_path if entry_messages(entry)
        ]
        source_to_target = {
            source["id"]: target["id"]
            for source, target in zip(
                source_message_entries,
                target_message_entries,
                strict=True,
            )
        }
        entries = list(target_tree.entries)
        known = {entry["id"] for entry in entries}
        parent_id = target_tree.leaf_id

        def append_clone_entry(kind, data):
            nonlocal parent_id
            entry = _new_entry(
                kind,
                target_store._redactor(deepcopy(data)),
                parent_id,
            )
            _validate_entry(entry, known)
            entries.append(entry)
            known.add(entry["id"])
            parent_id = entry["id"]

        latest_compaction = next(
            (
                entry
                for entry in reversed(tree.active_path)
                if entry["type"] == "compaction"
            ),
            None,
        )
        if latest_compaction is not None:
            data = deepcopy(latest_compaction["data"])
            old_kept = data.get("first_kept_entry_id", "")
            data["first_kept_entry_id"] = source_to_target.get(old_kept, "")
            data["reason"] = "worktree_clone"
            data["provider_usage"] = {}
            data["split_provider_usage"] = {}
            append_clone_entry("compaction", data)
        latest_branch = next(
            (
                entry
                for entry in reversed(tree.active_path)
                if entry["type"] == "branch_summary"
                and (
                    latest_compaction is None
                    or tree.active_path.index(entry)
                    > tree.active_path.index(latest_compaction)
                )
            ),
            None,
        )
        if latest_branch is not None:
            data = deepcopy(latest_branch["data"])
            data["abandoned_leaf_id"] = ""
            data["target_entry_id"] = ""
            data["provider_usage"] = {}
            append_clone_entry("branch_summary", data)
        if source_checkpoint is not None:
            cloned_checkpoint_id = f"{source_checkpoint_id}-clone-{uuid.uuid4().hex[:8]}"
            source_checkpoint.update(
                {
                    "checkpoint_id": cloned_checkpoint_id,
                    "parent_checkpoint_id": "",
                    "created_at": now(),
                    "worktree_identity_digest": target_tree.header["worktree_identity"][
                        "digest"
                    ],
                    "context_usage": {},
                    "key_files": [],
                    "read_files": [],
                    "modified_files": [],
                }
            )
            source_checkpoint.pop("runtime_identity", None)
            source_checkpoint.pop("freshness", None)
            append_clone_entry(
                "task_checkpoint",
                {
                    "checkpoint_id": cloned_checkpoint_id,
                    "checkpoint": source_checkpoint,
                },
            )
        rendered = _render_tree(target_tree.header, entries)
        if len(rendered) > MAX_SESSION_BYTES:
            raise ValueError("private file too large")
        final_tree = _parse_jsonl(rendered, clone_id)

        def validate_clone_identity():
            if os.path.lexists(target_store.legacy_path(clone_id)):
                raise ValueError("clone session id already exists")
            if worktree_identity(str(target_root)) != final_tree.header[
                "worktree_identity"
            ]:
                raise SessionFormatError("clone target worktree changed")

        with file_lock.locked_file(target_store.lock_path):
            write_private_bytes_atomic(
                target_store.path(clone_id),
                rendered,
                trusted_root=target_store.root,
                trusted_root_identity=target_store._root_identity,
                error="clone session id already exists",
                max_existing_bytes=MAX_SESSION_BYTES,
                require_absent=True,
                validate_commit=validate_clone_identity,
            )
        return {
            "session_id": clone_id,
            "workspace_root": str(target_root),
            "path": str(target_store.path(clone_id)),
        }

    def entries(self, session_id):
        return [deepcopy(entry) for entry in self.load_tree(session_id).entries]

    def context_view(self, session_id):
        return context_view_from_tree(self.load_tree(session_id))

    def inspect_readonly(self, session_id):
        """Read current or legacy state without triggering migration."""
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            if self.path(session_id).exists():
                raw = read_private_bytes(
                    self.path(session_id),
                    trusted_root=self.root,
                    trusted_root_identity=self._root_identity,
                    max_bytes=MAX_SESSION_BYTES,
                    harden=False,
                )
                version = _jsonl_format_version(raw)
                if version not in {
                    LEGACY_JSONL_SESSION_FORMAT_VERSION,
                    PREVIOUS_SESSION_FORMAT_VERSION,
                    WORKFLOW_SESSION_FORMAT_VERSION,
                    SESSION_FORMAT_VERSION,
                }:
                    raise SessionFormatError("invalid session header version")
                tree = _parse_jsonl(raw, session_id, version=version)
                storage = "current" if version == SESSION_FORMAT_VERSION else "legacy_jsonl"
                return storage, deepcopy(tree.projection), tree
            raw = read_private_bytes(
                self.legacy_path(session_id),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_SESSION_ENTRY_BYTES,
                harden=False,
            )
            payload = _decode_json_object(raw)
            _validate_legacy_payload(payload, session_id)
            return "legacy", deepcopy(payload), None

    def repair_tail(self, session_id):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            raw = read_private_bytes(
                self.path(session_id),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_SESSION_BYTES,
            )
            source_version = _jsonl_format_version(raw)
            if source_version in {
                LEGACY_JSONL_SESSION_FORMAT_VERSION,
                PREVIOUS_SESSION_FORMAT_VERSION,
                WORKFLOW_SESSION_FORMAT_VERSION,
            }:
                raise SessionMigrationRequired(session_id)
            if raw.endswith(b"\n"):
                return False
            valid_bytes = raw.rfind(b"\n") + 1
            if valid_bytes <= 0:
                raise SessionFormatError("session has no valid JSONL prefix")
            repaired = raw[:valid_bytes]
            version = _jsonl_format_version(repaired)
            if version not in {
                LEGACY_JSONL_SESSION_FORMAT_VERSION,
                PREVIOUS_SESSION_FORMAT_VERSION,
                WORKFLOW_SESSION_FORMAT_VERSION,
                SESSION_FORMAT_VERSION,
            }:
                raise SessionFormatError("invalid session header version")
            _parse_jsonl(repaired, session_id, version=version)
            write_private_bytes_atomic(
                self.path(session_id),
                repaired,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                error="session tail repair changed",
                max_existing_bytes=MAX_SESSION_BYTES,
            )
            self._tree_cache.pop(session_id, None)
            return True

    def _migrate_jsonl_unlocked(self, session_id, source_version, source_raw=None):
        if source_version not in {
            LEGACY_JSONL_SESSION_FORMAT_VERSION,
            PREVIOUS_SESSION_FORMAT_VERSION,
            WORKFLOW_SESSION_FORMAT_VERSION,
        }:
            raise SessionFormatError("invalid session migration source version")
        source = self.path(session_id)
        _harden_migration_source(
            source,
            root=self.root,
            root_identity=self._root_identity,
        )
        raw, source_signature = _stable_private_read(
            source,
            root=self.root,
            root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
            error="session changed during migration",
        )
        if source_raw is not None and raw != source_raw:
            raise SessionFormatError("session changed during migration")
        source_digest = hashlib.sha256(raw).digest()
        old_tree = _parse_jsonl(
            raw,
            session_id,
            version=source_version,
        )
        if source_version == LEGACY_JSONL_SESSION_FORMAT_VERSION and any(
            entry["type"] == "model_change" for entry in old_tree.entries
        ):
            raise UnsupportedLegacyEntry("model_change")
        rows = [_decode_json_object(line) for line in raw.splitlines()]
        migrated_rows = []
        for row in rows:
            migrated = deepcopy(row)
            migrated["format_version"] = SESSION_FORMAT_VERSION
            if migrated.get("type") == "session_info":
                state = migrated.get("data", {}).get("set", {})
                state["format_version"] = SESSION_FORMAT_VERSION
                state.pop("recovery", None)
            elif migrated.get("type") == "task_checkpoint":
                checkpoint = migrated.get("data", {}).get("checkpoint")
                if isinstance(checkpoint, dict):
                    checkpoint.pop("workspace_checkpoint_id", None)
            elif (
                source_version == WORKFLOW_SESSION_FORMAT_VERSION
                and migrated.get("type") == "workflow_mode_change"
            ):
                migrated["type"] = "permission_mode_change"
                legacy_mode = migrated["data"]["mode"]
                migrated["data"] = {
                    "mode": (
                        PermissionMode.DEFAULT.value
                        if legacy_mode == "act"
                        else PermissionMode.PLAN.value
                    ),
                    **(
                        {"pre_mode": PermissionMode.DEFAULT.value}
                        if legacy_mode != "act"
                        else {}
                    ),
                }
            elif (
                source_version == WORKFLOW_SESSION_FORMAT_VERSION
                and migrated.get("type") == "plan_update"
            ):
                migrated["type"] = "migration"
                migrated["data"] = {
                    "from_format": WORKFLOW_SESSION_FORMAT_VERSION,
                    "legacy_control": deepcopy(row["data"]),
                }
            migrated_rows.append(migrated)
        candidate_bytes = b"".join(_serialize_line(row) for row in migrated_rows)
        candidate_tree = _parse_jsonl(candidate_bytes, session_id)
        expected = deepcopy(old_tree.projection)
        if source_version == WORKFLOW_SESSION_FORMAT_VERSION:
            legacy_mode = expected.pop("workflow_mode", DEFAULT_WORKFLOW_MODE)
            expected.pop("active_plan", None)
            permission_mode = (
                PermissionMode.DEFAULT.value
                if legacy_mode == "act"
                else PermissionMode.PLAN.value
            )
        elif source_version == LEGACY_JSONL_SESSION_FORMAT_VERSION:
            permission_mode = PermissionMode.DEFAULT.value
        if source_version != PREVIOUS_SESSION_FORMAT_VERSION:
            expected.update(
                permission_mode=permission_mode,
                permission_rules={"allow": [], "ask": [], "deny": []},
                plan_text="",
                plan_revision=0,
                pre_plan_mode=(
                    PermissionMode.DEFAULT.value
                    if permission_mode == PermissionMode.PLAN.value
                    else ""
                ),
            )
        expected["format_version"] = SESSION_FORMAT_VERSION
        expected.pop("recovery", None)
        for checkpoint in expected.get("checkpoints", {}).get("items", {}).values():
            if isinstance(checkpoint, dict):
                checkpoint.pop("workspace_checkpoint_id", None)
        if not session_projections_equal(candidate_tree.projection, expected):
            raise SessionFormatError("session migration projection mismatch")

        backup_root = ensure_private_dir(self.root / "legacy-backups")
        backup_identity = private_directory_identity(backup_root)
        digest = hashlib.sha256(raw).hexdigest()
        backup_path = backup_root / f"{session_id}.{digest[:16]}.jsonl"
        _write_or_verify_backup(
            backup_path,
            raw,
            root=backup_root,
            root_identity=backup_identity,
            max_bytes=MAX_SESSION_BYTES,
        )

        candidate_path = self.candidate_path(session_id)
        write_private_bytes_atomic(
            candidate_path,
            candidate_bytes,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            error="session migration candidate changed",
            max_existing_bytes=MAX_SESSION_BYTES,
        )
        candidate_raw, candidate_signature = _stable_private_read(
            candidate_path,
            root=self.root,
            root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
            error="session migration candidate changed",
            expected_bytes=candidate_bytes,
        )
        _parse_jsonl(candidate_raw, session_id)
        final_source, _ = _stable_private_read(
            source,
            root=self.root,
            root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
            error="session changed during migration",
            expected_signature=source_signature,
        )
        if hashlib.sha256(final_source).digest() != source_digest:
            raise SessionFormatError("session changed during migration")
        parent_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            if private_file_signature(
                candidate_path,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
            ) != candidate_signature:
                raise SessionFormatError("session migration candidate changed")
            current = os.stat(
                candidate_path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != candidate_signature[:2]
            ):
                raise SessionFormatError("session migration candidate changed")
            os.replace(
                candidate_path.name,
                source.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self._tree_cache.pop(session_id, None)
        return source

    def _migrate_legacy_unlocked(self, session_id):
        canonical = self.path(session_id)
        if canonical.exists():
            return canonical
        legacy = self.legacy_path(session_id)
        _harden_migration_source(
            legacy,
            root=self.root,
            root_identity=self._root_identity,
        )
        raw, source_signature = _stable_private_read(
            legacy,
            root=self.root,
            root_identity=self._root_identity,
            max_bytes=MAX_SESSION_ENTRY_BYTES,
            error="session changed during migration",
        )
        source_digest = hashlib.sha256(raw).digest()
        payload = _decode_json_object(raw)
        _validate_legacy_payload(payload, session_id)
        migrated = deepcopy(payload)
        migrated["format_version"] = SESSION_FORMAT_VERSION
        migrated["permission_mode"] = PermissionMode.DEFAULT.value
        migrated["permission_rules"] = {"allow": [], "ask": [], "deny": []}
        migrated["plan_text"] = ""
        migrated["plan_revision"] = 0
        migrated["pre_plan_mode"] = ""
        migrated.pop("recovery", None)
        for checkpoint in migrated.get("checkpoints", {}).get("items", {}).values():
            if isinstance(checkpoint, dict):
                checkpoint.pop("workspace_checkpoint_id", None)

        backup_root = ensure_private_dir(self.root / "legacy-backups")
        backup_identity = private_directory_identity(backup_root)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        backup_path = backup_root / f"{session_id}.{digest}.json"
        _write_or_verify_backup(
            backup_path,
            raw,
            root=backup_root,
            root_identity=backup_identity,
            max_bytes=MAX_SESSION_ENTRY_BYTES,
        )

        header = {
            "record_type": SESSION_HEADER_RECORD_TYPE,
            "format_version": SESSION_FORMAT_VERSION,
            "id": session_id,
            "created_at": migrated["created_at"],
            "workspace_root": migrated["workspace_root"],
            "worktree_identity": worktree_identity(migrated["workspace_root"]),
        }
        _promote_legacy_working_state(
            migrated,
            header["worktree_identity"]["digest"],
        )
        initial_state = deepcopy(migrated)
        if _ordered_checkpoint_items(migrated.get("checkpoints", {})):
            initial_state["checkpoints"] = {"current_id": "", "items": {}}
        state = _new_entry("session_info", {"set": _state_values(initial_state)}, "")
        marker = _new_entry(
            "migration",
            {
                "from_format": LEGACY_SESSION_FORMAT_VERSION,
                "legacy_sha256": hashlib.sha256(raw).hexdigest(),
                "backup_name": backup_path.name,
            },
            state["id"],
        )
        entries = [state, marker]
        parent_id = marker["id"]
        checkpoint_entries = _task_checkpoint_entries(
            migrated.get("checkpoints", {}),
            parent_id,
            known_ids={entry["id"] for entry in entries},
        )
        entries.extend(checkpoint_entries)
        if checkpoint_entries:
            parent_id = checkpoint_entries[-1]["id"]
        entries.extend(_message_entries(migrated["messages"], parent_id))
        entries = _restore_expected_projection(header, entries, migrated)
        candidate_bytes = _render_tree(header, entries)
        _parse_jsonl(candidate_bytes, session_id)
        candidate_path = self.candidate_path(session_id)
        write_private_bytes_atomic(
            candidate_path,
            candidate_bytes,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            error="session migration candidate changed",
            max_existing_bytes=MAX_SESSION_BYTES,
        )
        candidate_raw, candidate_signature = _stable_private_read(
            candidate_path,
            root=self.root,
            root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
            error="session migration candidate changed",
            expected_bytes=candidate_bytes,
        )
        candidate_tree = _parse_jsonl(candidate_raw, session_id)
        if not session_projections_equal(candidate_tree.projection, migrated):
            raise SessionFormatError("session migration projection mismatch")
        parent_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            final_source, _ = _stable_private_read(
                legacy,
                root=self.root,
                root_identity=self._root_identity,
                max_bytes=MAX_SESSION_ENTRY_BYTES,
                error="session changed during migration",
                expected_signature=source_signature,
            )
            if hashlib.sha256(final_source).digest() != source_digest:
                raise SessionFormatError("session changed during migration")
            if private_file_signature(
                candidate_path,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
            ) != candidate_signature:
                raise SessionFormatError("session migration candidate changed")
            current = os.stat(
                candidate_path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != candidate_signature[:2]
            ):
                raise SessionFormatError("session migration candidate changed")
            os.replace(
                candidate_path.name,
                canonical.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            try:
                os.unlink(legacy.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_fd)
        signature = private_file_signature(
            canonical,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        self._tree_cache[session_id] = (signature, candidate_tree)
        return canonical

    def latest(self):
        files = []
        for pattern in ("*.jsonl", "*.json"):
            for path in self.root.glob(pattern):
                try:
                    require_regular_no_symlink(path)
                    signature = private_file_signature(
                        path,
                        trusted_root=self.root,
                        trusted_root_identity=self._root_identity,
                    )
                    expected_uid = (
                        os.geteuid() if hasattr(os, "geteuid") else signature[7]
                    )
                    if signature[6] != 0o600 or signature[7] != expected_uid:
                        continue
                    session_id = path.name.removesuffix(".jsonl").removesuffix(".json")
                    _session_id(session_id)
                    files.append((signature[3], session_id))
                except (OSError, ValueError):
                    continue
        files.sort()
        return files[-1][1] if files else None
