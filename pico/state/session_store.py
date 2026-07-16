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

from pico.state import file_lock
from pico.context.escaping import escape_pico_tags
from pico.agent.messages import MessageValidationError, message_content_text, validate_messages
from pico.agent.model_capabilities import estimate_text_tokens
from pico.security import (
    append_private_bytes,
    ensure_private_dir,
    ensure_private_file,
    harden_private_tree,
    private_file_signature,
    private_directory_identity,
    read_private_bytes,
    require_regular_no_symlink,
    write_private_bytes_atomic,
)
from pico.workspace import now


SESSION_RECORD_TYPE = "session"
SESSION_FORMAT_VERSION = 2
LEGACY_SESSION_FORMAT_VERSION = 1
SESSION_HEADER_RECORD_TYPE = "session_header"
SESSION_ENTRY_RECORD_TYPE = "session_entry"
MAX_SESSION_ENTRY_BYTES = 8 * 1024 * 1024
SESSION_SOFT_LIMIT_BYTES = 128 * 1024 * 1024
MAX_SESSION_BYTES = 512 * 1024 * 1024
MAX_REWIND_INTENT_BYTES = 64 * 1024
REWIND_INTENT_RECORD_TYPE = "rewind_intent"
REWIND_INTENT_FORMAT_VERSION = 2

ENTRY_TYPES = frozenset(
    {
        "message",
        "tool_exchange",
        "model_change",
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


class SessionFormatError(ValueError):
    """A Session artifact does not match the current on-disk contract."""


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
        "recovery",
        "runtime_identity",
    }
)
_OPTIONAL_FIELDS = frozenset({"provider_binding"})
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
    "recovery",
    "runtime_identity",
)
_DERIVED_CACHE_FIELDS = frozenset({"working_memory", "memory", "recently_recalled"})
_SESSION_INFO_FIELDS = (
    _REQUIRED_FIELDS | _OPTIONAL_FIELDS
) - {"messages"} - _DERIVED_CACHE_FIELDS
_MUTABLE_SESSION_INFO_FIELDS = frozenset(
    {"resume_state", "recovery", "runtime_identity"}
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
        "workspace_checkpoint_id",
        "worktree_identity_digest",
        "context_usage",
        "label",
        "trigger",
        "freshness",
        "summary",
        "runtime_identity",
    }
)
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
_REWIND_INTENT_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "session_id",
        "created_at",
        "old_leaf_id",
        "target_entry_id",
        "target_checkpoint_id",
        "workspace_checkpoint_id",
        "operation_id",
        "plan_digest",
        "worktree_identity_digest",
        "state",
        "restore_checkpoint_id",
        "restore_status",
        "branch_summary",
        "branch_summary_tokens",
        "branch_summary_focus",
        "branch_summary_provider_usage",
        "recovery_owner_id",
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


def _validate_rewind_intent(value, session_id):
    if not isinstance(value, dict) or value.keys() != _REWIND_INTENT_FIELDS:
        raise SessionFormatError("invalid rewind intent fields")
    if (
        value.get("record_type") != REWIND_INTENT_RECORD_TYPE
        or value.get("format_version") != REWIND_INTENT_FORMAT_VERSION
        or value.get("session_id") != session_id
    ):
        raise SessionFormatError("invalid rewind intent identity")
    string_fields = _REWIND_INTENT_FIELDS - {
        "format_version",
        "branch_summary_tokens",
        "branch_summary_provider_usage",
    }
    if any(not isinstance(value.get(key), str) for key in string_fields):
        raise SessionFormatError("invalid rewind intent string")
    if value["state"] not in {"prepared", "restored"}:
        raise SessionFormatError("invalid rewind intent state")
    if not re.fullmatch(r"rewind_[0-9a-f]{32}", value["operation_id"]):
        raise SessionFormatError("invalid rewind operation id")
    if not re.fullmatch(r"[0-9a-f]{64}", value["plan_digest"]):
        raise SessionFormatError("invalid rewind plan digest")
    if (
        type(value["branch_summary_tokens"]) is not int
        or value["branch_summary_tokens"] < 0
        or not isinstance(value["branch_summary_provider_usage"], dict)
    ):
        raise SessionFormatError("invalid rewind branch summary")
    return value


def _validate_projection(payload, session_id, *, version=SESSION_FORMAT_VERSION):
    if not isinstance(payload, dict):
        raise SessionFormatError("session payload must be an object")
    if not _REQUIRED_FIELDS <= payload.keys() or not payload.keys() <= (
        _REQUIRED_FIELDS | _OPTIONAL_FIELDS
    ):
        raise SessionFormatError("session payload fields do not match current format")
    if payload.get("record_type") != SESSION_RECORD_TYPE:
        raise SessionFormatError("invalid session record type")
    if type(payload.get("format_version")) is not int or payload["format_version"] != version:
        raise SessionFormatError("invalid session format version")
    if payload.get("id") != session_id:
        raise SessionFormatError("session id does not match file name")
    if any(
        not isinstance(payload.get(key), str)
        for key in ("id", "created_at", "workspace_root")
    ):
        raise SessionFormatError("invalid session string field")
    if any(not isinstance(payload.get(key), dict) for key in _DICT_FIELDS):
        raise SessionFormatError("invalid session object field")
    if not isinstance(payload.get("recently_recalled"), list):
        raise SessionFormatError("invalid session list field")
    binding = payload.get("provider_binding")
    if binding is not None and (
        not isinstance(binding, dict)
        or binding.keys()
        != {"protocol_family", "model", "endpoint_hash"}
        or any(not isinstance(value, str) or not value for value in binding.values())
        or binding["protocol_family"] not in _PROTOCOL_FAMILIES
        or re.fullmatch(r"sha256:[0-9a-f]{64}", binding["endpoint_hash"])
        is None
    ):
        raise SessionFormatError("invalid provider binding")
    identities = [payload["runtime_identity"]]
    items = payload["checkpoints"].get("items", {})
    if isinstance(items, dict):
        for checkpoint in items.values():
            if not isinstance(checkpoint, dict):
                raise SessionFormatError("invalid embedded checkpoint")
            if not checkpoint.keys() <= _TASK_CHECKPOINT_FIELDS:
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
    if not isinstance(values, dict) or not values.keys() <= _MUTABLE_SESSION_INFO_FIELDS:
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


def _validate_header(header, session_id):
    if not isinstance(header, dict) or header.keys() != _HEADER_FIELDS:
        raise SessionFormatError("invalid session header fields")
    if header.get("record_type") != SESSION_HEADER_RECORD_TYPE:
        raise SessionFormatError("invalid session header type")
    if header.get("format_version") != SESSION_FORMAT_VERSION:
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


def _validate_entry(entry, known_ids):
    if not isinstance(entry, dict) or entry.keys() != _ENTRY_FIELDS:
        raise SessionFormatError("invalid session entry fields")
    if entry.get("record_type") != SESSION_ENTRY_RECORD_TYPE:
        raise SessionFormatError("invalid session entry type")
    if entry.get("format_version") != SESSION_FORMAT_VERSION:
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
    if entry.get("type") not in ENTRY_TYPES:
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
            or not checkpoint.keys() <= _TASK_CHECKPOINT_FIELDS
        ):
            raise SessionFormatError("invalid task checkpoint entry data")
    return entry


def _base_projection(header):
    return {
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
        "recovery": {"current_checkpoint_id": ""},
        "runtime_identity": {},
    }


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


def _apply_entry(projection, entry):
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
    elif kind == "session_info":
        values = data.get("set")
        if not isinstance(values, dict) or "messages" in values:
            raise SessionFormatError("invalid session info entry")
        if not values.keys() <= _SESSION_INFO_FIELDS:
            raise SessionFormatError("invalid session info field")
        projection.update(deepcopy(values))
    elif kind == "task_checkpoint":
        checkpoint_id = data.get("checkpoint_id")
        checkpoint = data.get("checkpoint")
        if not isinstance(checkpoint_id, str) or not isinstance(checkpoint, dict):
            raise SessionFormatError("invalid task checkpoint entry")
        checkpoints = projection.setdefault("checkpoints", {"current_id": "", "items": {}})
        checkpoints.setdefault("items", {})[checkpoint_id] = deepcopy(checkpoint)
        checkpoints["current_id"] = checkpoint_id
        goal = str(
            checkpoint.get("goal", checkpoint.get("current_goal", "")) or ""
        )
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
        workspace_checkpoint_id = str(
            checkpoint.get("workspace_checkpoint_id", "") or ""
        )
        if workspace_checkpoint_id:
            projection["recovery"] = {
                "current_checkpoint_id": workspace_checkpoint_id
            }
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
        "compaction": "pico:session_summary",
        "split_turn": "pico:split_turn_summary",
        "branch": "pico:branch_summary",
    }
    origins = {
        "compaction": "compaction_summary",
        "split_turn": "split_turn_summary",
        "branch": "branch_summary",
    }
    tag = tags[kind]
    safe_summary = escape_pico_tags(str(summary).strip())
    return {
        "role": "user",
        "content": (
            f"<{tag}>\n"
            + safe_summary
            + f"\n</{tag}>"
        ),
        "_pico_meta": {
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
            raw_split_tokens if type(raw_split_tokens) is int and raw_split_tokens >= 0 else 0
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
        messages.append(
            _summary_message(summary, compaction_id, compaction_created_at)
        )
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
        _apply_entry(projection, entry)
    _validate_projection(projection, header["id"])
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
        dict.fromkeys(
            str(path).strip()
            for path in recent_files
            if str(path).strip()
        )
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
    checkpoint_id = "ckpt_migrated_" + hashlib.sha256(
        seed.encode("utf-8")
    ).hexdigest()[:16]
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
        "workspace_checkpoint_id": "",
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
    return b"".join([_serialize_line(header), *(_serialize_line(item) for item in entries)])


def _parse_jsonl(raw, session_id):
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
    header = _validate_header(_decode_json_object(raw_lines[0]), session_id)
    if worktree_identity(header["workspace_root"]) != header["worktree_identity"]:
        raise SessionFormatError("session worktree identity mismatch")
    entries = []
    known_ids = set()
    for line in raw_lines[1:]:
        entry = _validate_entry(_decode_json_object(line), known_ids)
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

    A checkpoint carries the runtime/recovery state that was true when it was
    created.  When importing a full projection, that historical state must not
    replace the caller's current state.  Keep the checkpoint as a first-class
    entry, then restore only the fields whose projected values differ.
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
        _apply_entry(projection, entry)
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

    def rewind_intent_path(self, session_id):
        return self.root / f"{_session_id(session_id)}.rewind-intent.json"

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

    def load_tree(self, session_id, *, migrate=True):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            if not self.path(session_id).exists() and migrate:
                self._migrate_legacy_unlocked(session_id)
            return self._read_tree_unlocked(session_id)

    def load(self, session_id):
        return deepcopy(self.load_tree(session_id).projection)

    def _load_unlocked(self, session_id):
        session_id = _session_id(session_id)
        if not self.path(session_id).exists():
            self._migrate_legacy_unlocked(session_id)
        return deepcopy(self._read_tree_unlocked(session_id).projection)

    def _write_new_tree_unlocked(self, session, *, migration=False):
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
        if cached is not None:
            tree = _extend_tree(cached[1], entries)
            signature = private_file_signature(
                path,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
            )
            self._tree_cache[session_id] = (signature, tree)
        else:
            self._tree_cache.pop(session_id, None)
        size = path.stat().st_size
        if size >= SESSION_SOFT_LIMIT_BYTES and session_id not in self._soft_limit_warned:
            warnings.warn(
                f"session {session_id} exceeds 128 MiB; compact or clone it",
                RuntimeWarning,
                stacklevel=2,
            )
            self._soft_limit_warned.add(session_id)
        return path

    def save(self, session):
        if not isinstance(session, dict):
            raise SessionFormatError("session payload must be an object")
        candidate = self._redactor(deepcopy(session))
        candidate.pop("_recall_errors", None)
        session_id = _session_id(candidate.get("id"))
        candidate["format_version"] = SESSION_FORMAT_VERSION
        _validate_projection(candidate, session_id)
        with file_lock.locked_file(self.lock_path):
            canonical = self.path(session_id)
            if not canonical.exists():
                if self.legacy_path(session_id).exists():
                    self._migrate_legacy_unlocked(session_id)
                else:
                    return self._write_new_tree_unlocked(candidate)
            tree = self._read_tree_unlocked(session_id)
            if tree.header["workspace_root"] != candidate["workspace_root"]:
                raise SessionFormatError("session workspace root changed")
            current = tree.projection
            if current.get("provider_binding") != candidate.get("provider_binding"):
                raise SessionFormatError("session provider binding changed")
            parent_id = tree.leaf_id
            entries = []
            current_messages = current["messages"]
            candidate_messages = candidate["messages"]
            if candidate_messages[: len(current_messages)] == current_messages:
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
                # A task checkpoint deliberately projects its historical runtime
                # identity and recovery link onto the active branch.  ``save`` is
                # the compatibility boundary for callers that still edit a full
                # projection, so restore the caller's canonical session state
                # after encoding those checkpoint entries.
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
                message_entries = _message_entries(candidate_messages, parent_id)
                entries.extend(message_entries)
                changed = {}
                if message_entries:
                    parent_id = message_entries[-1]["id"]
            if changed:
                info = _new_entry("session_info", {"set": changed}, parent_id)
                entries.append(info)
            path = self._append_entries_unlocked(session_id, entries)
            persisted = self._read_tree_unlocked(session_id).projection
            if _persistent_projection(persisted) != _persistent_projection(candidate):
                raise SessionFormatError("session tree projection mismatch")
            return path

    def append_messages(self, session_id, messages, *, state_updates=None):
        """Atomically append one message batch without rescanning full history.

        A tool call/result pair is encoded as one ``tool_exchange`` entry.  Only
        the new batch and the small canonical runtime state are validated here;
        the already validated cached Session Tree remains immutable.
        """
        session_id = _session_id(session_id)
        safe_messages = self._redactor(deepcopy(list(messages or ())))
        try:
            validate_messages(safe_messages, require_meta=True)
        except MessageValidationError as exc:
            raise SessionFormatError(str(exc)) from None
        if not safe_messages and not state_updates:
            return self.path(session_id)
        safe_state = self._redactor(deepcopy(state_updates or {}))
        _validate_session_info_updates(safe_state)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            parent_id = tree.leaf_id
            entries = _message_entries(safe_messages, parent_id)
            if entries:
                parent_id = entries[-1]["id"]
            changed = {
                key: value
                for key, value in safe_state.items()
                if value != tree.projection.get(key)
            }
            if changed:
                entries.append(
                    _new_entry("session_info", {"set": changed}, parent_id)
                )
            return self._append_entries_unlocked(session_id, entries)

    def append_control(self, session_id, kind, data, *, parent_id=None):
        if kind not in ENTRY_TYPES - {"message", "tool_exchange", "session_info"}:
            raise ValueError("invalid control entry type")
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            target = tree.leaf_id if parent_id is None else str(parent_id)
            known = {entry["id"] for entry in tree.entries}
            if target and target not in known:
                raise SessionFormatError("unknown parent entry")
            safe_data = self._redactor(deepcopy(data))
            if not isinstance(safe_data, dict):
                raise SessionFormatError("control entry data must be an object")
            entry = _new_entry(kind, safe_data, target)
            _validate_entry(entry, known)
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

    def write_rewind_intent(self, session_id, intent):
        session_id = _session_id(session_id)
        safe = self._redactor(deepcopy(intent))
        _validate_rewind_intent(safe, session_id)
        rendered = json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(rendered) > MAX_REWIND_INTENT_BYTES:
            raise ValueError("rewind intent too large")
        with file_lock.locked_file(self.lock_path, require_existing=True):
            tree = self._read_tree_unlocked(session_id)
            if safe["worktree_identity_digest"] != tree.header[
                "worktree_identity"
            ]["digest"]:
                raise SessionFormatError("rewind intent worktree mismatch")
            write_private_bytes_atomic(
                self.rewind_intent_path(session_id),
                rendered,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                error="rewind intent changed",
                max_existing_bytes=MAX_REWIND_INTENT_BYTES,
            )
        return deepcopy(safe)

    def load_rewind_intent(self, session_id):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            try:
                raw = read_private_bytes(
                    self.rewind_intent_path(session_id),
                    trusted_root=self.root,
                    trusted_root_identity=self._root_identity,
                    max_bytes=MAX_REWIND_INTENT_BYTES,
                )
            except FileNotFoundError:
                return None
            return deepcopy(
                _validate_rewind_intent(_decode_json_object(raw), session_id)
            )

    def clear_rewind_intent(self, session_id):
        session_id = _session_id(session_id)
        path = self.rewind_intent_path(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            descriptor = os.open(
                self.root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                try:
                    current = os.stat(
                        path.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return False
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise SessionFormatError("unsafe rewind intent")
                os.unlink(path.name, dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return True

    def rewind(
        self,
        session_id,
        entry_id,
        *,
        summary="",
        workspace_checkpoint_id="",
        restore_checkpoint_id="",
        target_checkpoint_id="",
    ):
        data = {
            "target_entry_id": str(entry_id or ""),
            "summary": str(summary or ""),
            "workspace_checkpoint_id": str(workspace_checkpoint_id or ""),
            "restore_checkpoint_id": str(restore_checkpoint_id or ""),
            "target_checkpoint_id": str(target_checkpoint_id or ""),
        }
        return self.append_control(
            session_id,
            "rewind",
            data,
            parent_id=str(entry_id or ""),
        )

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
        """Clone the active branch while clearing workspace-bound recovery state."""
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
                "recovery": {"current_checkpoint_id": ""},
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
            target_root / ".pico" / "sessions",
            redactor=self._redactor,
        )
        if target_store.path(clone_id).exists() or target_store.legacy_path(clone_id).exists():
            raise ValueError("clone session id already exists")
        target_store.save(projection)
        target_tree = target_store.load_tree(clone_id)

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
            target_store.append_control(clone_id, "compaction", data)
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
            target_store.append_control(clone_id, "branch_summary", data)
        if source_checkpoint is not None:
            cloned_checkpoint_id = (
                f"{source_checkpoint_id}-clone-{uuid.uuid4().hex[:8]}"
            )
            source_checkpoint.update(
                {
                    "checkpoint_id": cloned_checkpoint_id,
                    "parent_checkpoint_id": "",
                    "created_at": now(),
                    "workspace_checkpoint_id": "",
                    "worktree_identity_digest": target_store.load_tree(
                        clone_id
                    ).header["worktree_identity"]["digest"],
                    "context_usage": {},
                    "key_files": [],
                    "read_files": [],
                    "modified_files": [],
                }
            )
            source_checkpoint.pop("runtime_identity", None)
            source_checkpoint.pop("freshness", None)
            target_store.append_task_checkpoint(clone_id, source_checkpoint)
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
                tree = self._read_tree_unlocked(session_id)
                return "current", deepcopy(tree.projection), tree
            raw = read_private_bytes(
                self.legacy_path(session_id),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_SESSION_ENTRY_BYTES,
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
            if raw.endswith(b"\n"):
                return False
            valid_bytes = raw.rfind(b"\n") + 1
            if valid_bytes <= 0:
                raise SessionFormatError("session has no valid JSONL prefix")
            repaired = raw[:valid_bytes]
            _parse_jsonl(repaired, session_id)
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

    def _migrate_legacy_unlocked(self, session_id):
        canonical = self.path(session_id)
        if canonical.exists():
            return canonical
        legacy = self.legacy_path(session_id)
        raw = read_private_bytes(
            legacy,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_bytes=MAX_SESSION_ENTRY_BYTES,
        )
        payload = _decode_json_object(raw)
        _validate_legacy_payload(payload, session_id)
        migrated = deepcopy(payload)
        migrated["format_version"] = SESSION_FORMAT_VERSION

        backup_root = ensure_private_dir(self.root / "legacy-backups")
        backup_identity = private_directory_identity(backup_root)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        backup_path = backup_root / f"{session_id}.{digest}.json"
        if not backup_path.exists():
            write_private_bytes_atomic(
                backup_path,
                raw,
                trusted_root=backup_root,
                trusted_root_identity=backup_identity,
                error="legacy session backup changed",
                max_existing_bytes=MAX_SESSION_ENTRY_BYTES,
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
        candidate_raw = read_private_bytes(
            candidate_path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_bytes=MAX_SESSION_BYTES,
        )
        candidate_tree = _parse_jsonl(candidate_raw, session_id)
        if not session_projections_equal(candidate_tree.projection, migrated):
            raise SessionFormatError("session migration projection mismatch")
        parent_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            before = os.stat(candidate_path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SessionFormatError("unsafe session migration candidate")
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
                    safe = ensure_private_file(require_regular_no_symlink(path))
                    session_id = safe.name.removesuffix(".jsonl").removesuffix(".json")
                    _session_id(session_id)
                    files.append((safe.stat().st_mtime_ns, session_id))
                except (OSError, ValueError):
                    continue
        files.sort()
        return files[-1][1] if files else None
