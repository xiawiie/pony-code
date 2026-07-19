"""Read-only inspection for legacy and JSONL Session Tree artifacts."""

from __future__ import annotations

from pathlib import Path
import re

from pony.agent.messages import MessageValidationError, validate_messages
from pony.security.private_files import private_directory_identity
from pony.state.session_store import (
    LEGACY_JSONL_SESSION_FORMAT_VERSION,
    LEGACY_SESSION_FORMAT_VERSION,
    PREVIOUS_SESSION_FORMAT_VERSION,
    SESSION_FORMAT_VERSION,
    SESSION_RECORD_TYPE,
    SessionFormatError,
    SessionStore,
)
from pony.tools.permissions import PermissionMode


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _pair_count(messages):
    return sum(
        1
        for message in messages
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_use"
    )


def _readonly_store(sessions_root):
    """Build a store view without creating, hardening, or migrating anything."""
    store = object.__new__(SessionStore)
    store.root = sessions_root
    store._root_identity = private_directory_identity(sessions_root)
    store.lock_path = sessions_root / ".session_store.lock"
    store._tree_cache = {}
    return store


def load_session_readonly(session_id, sessions_root):
    """Load a legacy projection or JSONL tree without migration or writes."""
    return _readonly_store(Path(sessions_root)).inspect_readonly(session_id)


def resolve_session_id_readonly(session_id, sessions_root):
    """Resolve ``latest`` from artifact metadata without opening Session content."""
    if session_id != "latest":
        return session_id
    latest = _readonly_store(Path(sessions_root)).latest()
    if latest is None:
        raise FileNotFoundError("no sessions")
    return latest


def _tree_facts(tree):
    if tree is None:
        return {
            "entries": 0,
            "active_path": 0,
            "leaf": "-",
            "branch_points": 0,
            "compactions": 0,
            "task_checkpoints": 0,
        }
    child_counts = {}
    for entry in tree.entries:
        parent_id = entry["parent_id"]
        child_counts[parent_id] = child_counts.get(parent_id, 0) + 1
    return {
        "entries": len(tree.entries),
        "active_path": len(tree.active_path),
        "leaf": tree.leaf_id or "-",
        "branch_points": sum(count > 1 for count in child_counts.values()),
        "compactions": sum(entry["type"] == "compaction" for entry in tree.entries),
        "task_checkpoints": sum(
            entry["type"] == "task_checkpoint" for entry in tree.entries
        ),
    }


def session_inspection_data(session_id, sessions_root):
    """Return bounded Session facts without triggering legacy migration."""
    session_id = resolve_session_id_readonly(str(session_id or ""), sessions_root)
    session_id = str(session_id or "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise FileNotFoundError("session not found")
    sessions_root = Path(sessions_root)
    current_path = sessions_root / f"{session_id}.jsonl"
    legacy_path = sessions_root / f"{session_id}.json"
    try:
        current_exists = current_path.lstat() is not None
    except FileNotFoundError:
        current_exists = False
    except OSError:
        raise SessionFormatError("unsafe session artifact") from None
    try:
        legacy_exists = legacy_path.lstat() is not None
    except FileNotFoundError:
        legacy_exists = False
    except OSError:
        raise SessionFormatError("unsafe session artifact") from None
    if not current_exists and not legacy_exists:
        raise FileNotFoundError("session not found")

    try:
        storage, session, tree = load_session_readonly(session_id, sessions_root)
    except FileNotFoundError as exc:
        # An artifact found above but unavailable to the trusted reader is unsafe.
        raise SessionFormatError("unsafe session artifact") from exc

    if not isinstance(session, dict):
        raise SessionFormatError("session must be an object")
    record_type = session.get("record_type")
    version = session.get("format_version")
    expected_versions = {
        "current": {SESSION_FORMAT_VERSION},
        "legacy_jsonl": {
            LEGACY_JSONL_SESSION_FORMAT_VERSION,
            PREVIOUS_SESSION_FORMAT_VERSION,
        },
        "legacy": {LEGACY_SESSION_FORMAT_VERSION},
    }.get(storage)
    valid_version = (
        record_type == SESSION_RECORD_TYPE
        and type(version) is int
        and version in expected_versions
    )
    messages = session.get("messages")
    facts = _tree_facts(tree)
    if not valid_version:
        raise SessionFormatError("invalid schema version")
    validate_messages(messages, require_meta=True)

    permission_mode = session.get("permission_mode")
    if storage == "legacy_jsonl":
        permission_mode = (
            PermissionMode.DEFAULT.value
            if version == LEGACY_JSONL_SESSION_FORMAT_VERSION
            or session.get("workflow_mode") == "act"
            else PermissionMode.PLAN.value
        )
    elif storage == "legacy":
        permission_mode = PermissionMode.DEFAULT.value
    recovery = session.get("recovery")
    checkpoints = session.get("checkpoints")
    migration = "not_required" if storage == "current" else "required_on_resume"
    if (
        version == LEGACY_JSONL_SESSION_FORMAT_VERSION
        and any(entry["type"] == "model_change" for entry in tree.entries)
    ):
        migration = "unsupported_legacy_entry"
    return {
        "session_id": session_id,
        "storage": storage,
        "record_type": record_type,
        "format_version": version,
        "migration": migration,
        "permission_mode": permission_mode,
        "checkpoint": {
            "task": "present"
            if isinstance(checkpoints, dict) and checkpoints.get("current_id")
            else "none",
            "workspace_recovery": "linked"
            if isinstance(recovery, dict) and recovery.get("current_checkpoint_id")
            else "none",
        },
        "messages": len(messages),
        "role_sequence": [
            role if (role := message.get("role")) in {"user", "assistant"} else "?"
            for message in messages
            if isinstance(message, dict)
        ],
        **facts,
        "tool_pairs": _pair_count(messages),
        "orphans": 0,
        "invariants": "ok",
    }


def render_session_inspection(data):
    checkpoint = data["checkpoint"]
    migration = data["migration"].replace("_", " ")
    if data["migration"] == "required_on_resume":
        migration = "required on explicit resume"
    return "\n".join(
        [
            f"session: {data['session_id']}",
            f"storage: {data['storage']}",
            f"record_type: {data['record_type']}",
            f"format_version: {data['format_version']}",
            f"migration: {migration}",
            f"permission_mode: {data['permission_mode']}",
            f"checkpoint: {checkpoint['task']}",
            f"workspace_recovery: {checkpoint['workspace_recovery']}",
            f"messages: {data['messages']}",
            f"role_sequence: {' -> '.join(data['role_sequence'])}",
            f"entries: {data['entries']}",
            f"active_path_entries: {data['active_path']}",
            f"active_leaf: {data['leaf']}",
            f"branch_points: {data['branch_points']}",
            f"compactions: {data['compactions']}",
            f"task_checkpoints: {data['task_checkpoints']}",
            f"tool_pairs: {data['tool_pairs']}",
            f"orphans: {data['orphans']}",
            f"invariants: {data['invariants']}",
        ]
    )


def inspect_session(session_id, sessions_root):
    """Return ``(ok, report_str)`` without triggering legacy migration."""
    try:
        data = session_inspection_data(session_id, sessions_root)
    except FileNotFoundError:
        return False, f"session not found: {session_id}"
    except (MessageValidationError, OSError, ValueError, SessionFormatError):
        return False, f"failed to read session {session_id}: unsafe session artifact"
    return True, render_session_inspection(data)


def _tree_report(session_id, sessions_root):
    try:
        storage, _, tree = _readonly_store(Path(sessions_root)).inspect_readonly(
            session_id
        )
    except (FileNotFoundError, OSError, ValueError, SessionFormatError):
        return False, f"failed to read session {session_id}: unsafe session artifact"
    if storage == "legacy" or tree is None:
        return False, f"session {session_id} is legacy; resume it once to migrate"
    active = {entry["id"] for entry in tree.active_path}
    children = {}
    for entry in tree.entries:
        children.setdefault(entry["parent_id"], []).append(entry)
    lines = [
        f"session: {session_id}",
        f"format_version: {tree.header['format_version']}",
        f"active_leaf: {tree.leaf_id or '-'}",
        f"entries: {len(tree.entries)}",
    ]

    def walk(parent_id, depth):
        for entry in children.get(parent_id, []):
            marker = "*" if entry["id"] in active else " "
            lines.append(f"{marker} {'  ' * depth}{entry['id']} {entry['type']}")
            walk(entry["id"], depth + 1)

    walk("", 0)
    return True, "\n".join(lines)


def _store_for_write(sessions_root, redactor):
    return SessionStore(sessions_root, redactor=redactor)


def _option_value(argv, name):
    prefix = name + "="
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _rewind_flags(tokens):
    summary = False
    focus = ""
    for token in tokens:
        if token == "--summary":
            summary = True
        elif token.startswith("--summary="):
            summary = True
            focus = token.partition("=")[2]
        else:
            raise ValueError(f"unknown rewind option: {token}")
    return summary, focus


def handle_session_command(
    argv,
    sessions_root=None,
    redactor=None,
    agent_factory=None,
    raise_typed_errors=False,
):
    """CLI entry point for Session Tree inspection and explicit mutations."""
    if sessions_root is None:
        sessions_root = Path.cwd() / ".pony" / "sessions"
    argv = list(argv)
    if len(argv) < 2:
        print(
            "usage: pony session {inspect|tree|compact|checkpoint|fork|rewind|label|clone|tail-repair} <session-id>"
        )
        return 2
    command, session_id = argv[:2]
    report = ""
    ok = False
    try:
        if command == "inspect" and len(argv) == 2:
            ok, report = inspect_session(session_id, sessions_root=sessions_root)
        elif command == "tree" and len(argv) == 2:
            ok, report = _tree_report(session_id, sessions_root)
        elif command == "fork" and len(argv) == 3:
            entry = _store_for_write(sessions_root, redactor).fork(
                session_id,
                argv[2],
            )
            ok = True
            report = f"forked: {entry['id']}\nparent: {entry['parent_id']}"
        elif command == "checkpoint" and agent_factory is not None:
            checkpoint = agent_factory(session_id).create_manual_checkpoint(
                " ".join(argv[2:]).strip()
            )
            ok = True
            report = (
                f"checkpoint: {checkpoint['checkpoint_id']}\n"
                f"label: {checkpoint.get('label', '') or '-'}"
            )
        elif command == "rewind" and len(argv) >= 3:
            summary, focus = _rewind_flags(argv[3:])
            if summary:
                if agent_factory is None:
                    raise ValueError("summary rewind requires a model runtime")
                result = agent_factory(session_id).rewind_session(
                    argv[2],
                    summary=True,
                    focus=focus,
                )
                entry = result.rewind_entry
                report = (
                    f"rewound: {entry['id']}\nparent: {entry['parent_id']}\n"
                    f"branch_summary_tokens: {result.summary_tokens}"
                )
                ok = True
            else:
                entry = _store_for_write(sessions_root, redactor).rewind(
                    session_id,
                    argv[2],
                )
                report = f"rewound: {entry['id']}\nparent: {entry['parent_id']}"
                ok = True
        elif command == "label" and len(argv) >= 3:
            entry_id = _option_value(argv[3:], "--entry")
            label = argv[2]
            entry = _store_for_write(sessions_root, redactor).label(
                session_id,
                label,
                entry_id=entry_id,
            )
            ok = True
            report = f"labeled: {entry['id']}\nlabel: {label}"
        elif command == "clone":
            target = _option_value(argv[2:], "--to-worktree")
            new_id = _option_value(argv[2:], "--new-session-id")
            if not target:
                raise ValueError("clone requires --to-worktree PATH")
            result = _store_for_write(sessions_root, redactor).clone_to_worktree(
                session_id,
                target,
                new_session_id=new_id,
            )
            ok = True
            report = (
                f"cloned_session: {result['session_id']}\n"
                f"workspace_root: {result['workspace_root']}\npath: {result['path']}"
            )
        elif command == "tail-repair" and len(argv) == 3 and argv[2] == "--yes":
            repaired = _store_for_write(sessions_root, redactor).repair_tail(session_id)
            ok = True
            report = "tail_repaired: yes" if repaired else "tail_repaired: not_needed"
        elif command == "compact" and agent_factory is not None:
            focus = " ".join(argv[2:]).strip()
            result = agent_factory(session_id).compact_session(
                focus=focus,
                reason="manual_cli",
            )
            ok = True
            report = (
                f"compaction_entry: {result.entry['id']}\n"
                f"tokens_before: {result.tokens_before}\n"
                f"tokens_after: {result.tokens_after}\n"
                f"compression_ratio: {result.compression_ratio:.4f}"
            )
        else:
            print(
                "usage: pony session {inspect|tree|compact|checkpoint|fork|rewind|label|clone|tail-repair} <session-id>"
            )
            return 2
    except (OSError, RuntimeError, ValueError, SessionFormatError) as exc:
        if raise_typed_errors and getattr(exc, "code", ""):
            raise
        report = f"session command failed: {type(exc).__name__}: {exc}"
        ok = False
    if redactor is not None:
        report = redactor(report)
    print(report)
    return 0 if ok else 1
