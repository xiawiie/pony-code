"""从真实的 Tool Change Record 汇成 Checkpoint Record。

Phase 1 我们只需要两种 checkpoint：
    turn      —— 一次模型 turn 结束后写下的“这次 turn 改了什么”
    restore   —— apply_restore 之后写下的“恢复前后 workspace 长什么样”

会话上下文里“当前 checkpoint”不落在 checkpoint 记录本身，而是挂在 session 字典的
`recovery.current_checkpoint_id` 上。用两个辅助函数读写。
"""

from pathlib import Path
import re
import stat

from pony.recovery.models import new_checkpoint_record, new_id
from pony.recovery.paths import normalize_workspace_relative_path


_TERMINAL_TOOL_STATUSES = {
    "finalized",
    "error",
    "partial_success",
    "interrupted",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def validate_file_entry(entry):
    if not isinstance(entry, dict):
        return "invalid_file_entry"
    try:
        normalized = normalize_workspace_relative_path(entry.get("path", ""))
    except ValueError:
        return "invalid_path"
    if normalized != entry.get("path"):
        return "invalid_path"
    required = {
        "change_kind",
        "snapshot_eligible",
        "ineligible_reason",
        "before_exists",
        "before_blob_ref",
        "before_hash",
        "before_mode",
        "after_exists",
        "after_blob_ref",
        "after_hash",
        "after_mode",
        "expected_current_hash",
        "source_tool_change_ids",
    }
    if not required <= set(entry):
        return "invalid_file_entry"
    eligible = entry["snapshot_eligible"]
    ineligible_reason = entry["ineligible_reason"]
    if (
        type(eligible) is not bool
        or not isinstance(ineligible_reason, str)
        or (eligible and ineligible_reason != "")
        or (not eligible and not ineligible_reason)
    ):
        return "invalid_eligibility"
    before_exists = entry["before_exists"]
    after_exists = entry["after_exists"]
    if type(before_exists) is not bool or type(after_exists) is not bool:
        return "invalid_file_entry"
    mode_unknown = (
        eligible is False
        and ineligible_reason == "mode_unknown"
        and entry.get("before_mode") is None
        and entry.get("after_mode") is None
    )
    for prefix, exists in (("before", before_exists), ("after", after_exists)):
        value_hash = entry.get(prefix + "_hash")
        blob_ref = entry.get(prefix + "_blob_ref")
        mode = entry.get(prefix + "_mode")
        if not exists:
            if value_hash != "" or blob_ref != "" or mode is not None:
                return "invalid_absent_state"
            continue
        if not (mode is None and mode_unknown) and (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or stat.S_IMODE(mode) != mode
        ):
            return "invalid_mode"
        if value_hash and not _sha256(value_hash):
            return "invalid_hash"
        if blob_ref and not _sha256(blob_ref):
            return "invalid_blob_ref"
        if blob_ref and blob_ref != value_hash:
            return "blob_ref_hash_mismatch"
        if entry.get("snapshot_eligible") and (
            not value_hash or not blob_ref
        ):
            return "missing_eligible_state"
    expected = entry.get("expected_current_hash")
    if after_exists and expected != entry.get("after_hash"):
        return "expected_after_hash_mismatch"
    if not after_exists and expected != "":
        return "expected_after_hash_mismatch"
    expected_kind = (
        "created"
        if not before_exists and after_exists
        else "deleted"
        if before_exists and not after_exists
        else "modified"
    )
    if entry.get("change_kind") != expected_kind:
        return "change_kind_exists_mismatch"
    sources = entry.get("source_tool_change_ids")
    if not isinstance(sources, list) or any(
        not isinstance(item, str)
        or item in {"", ".", ".."}
        or _SAFE_ID.fullmatch(item) is None
        for item in sources
    ):
        return "invalid_sources"
    return ""


def _review_entry(entries, reason):
    first = entries[0]
    last = entries[-1]
    try:
        path = normalize_workspace_relative_path(first.get("path", ""))
    except (AttributeError, ValueError):
        return None
    before_exists = first.get("before_exists")
    after_exists = last.get("after_exists")
    if type(before_exists) is not bool or type(after_exists) is not bool:
        return None

    def state(entry, prefix, exists):
        if not exists:
            return "", "", None
        value_hash = entry.get(prefix + "_hash", "")
        if not _sha256(value_hash):
            value_hash = ""
        blob_ref = entry.get(prefix + "_blob_ref", "")
        if not _sha256(blob_ref) or blob_ref != value_hash:
            blob_ref = ""
        mode = entry.get(prefix + "_mode")
        if (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or stat.S_IMODE(mode) != mode
        ):
            mode = None
        return blob_ref, value_hash, mode

    before_blob_ref, before_hash, before_mode = state(
        first, "before", before_exists
    )
    after_blob_ref, after_hash, after_mode = state(last, "after", after_exists)
    result = {
        "path": path,
        "change_kind": (
            "created"
            if not before_exists and after_exists
            else "deleted"
            if before_exists and not after_exists
            else "modified"
        ),
        "snapshot_eligible": False,
        "ineligible_reason": reason,
        "before_exists": before_exists,
        "before_blob_ref": before_blob_ref,
        "before_hash": before_hash,
        "before_mode": before_mode,
        "after_exists": after_exists,
        "after_blob_ref": after_blob_ref,
        "after_hash": after_hash,
        "after_mode": after_mode,
        "expected_current_hash": after_hash if after_exists else "",
    }
    result["source_tool_change_ids"] = []
    for entry in entries:
        for source_id in entry.get("source_tool_change_ids", []) or []:
            if (
                isinstance(source_id, str)
                and source_id not in {"", ".", ".."}
                and _SAFE_ID.fullmatch(source_id) is not None
                and source_id not in result["source_tool_change_ids"]
            ):
                result["source_tool_change_ids"].append(source_id)
    return result


def coalesce_file_entries(entries):
    grouped = {}
    order = []
    for entry in entries or []:
        path = str(entry.get("path", "")) if isinstance(entry, dict) else ""
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append(dict(entry) if isinstance(entry, dict) else {})
    merged = []
    for path in order:
        group = grouped[path]
        reasons = [validate_file_entry(item) for item in group]
        if any(reasons):
            reason = next(value for value in reasons if value)
            reason = "discontinuous_history"
            review = _review_entry(group, reason)
            if review is not None:
                merged.append(review)
            continue
        if any(item.get("ineligible_reason") == "mode_unknown" for item in group):
            review = _review_entry(group, "mode_unknown")
            if review is not None:
                review["before_mode"] = None
                review["after_mode"] = None
                merged.append(review)
            continue
        continuous = all(
            (
                left["after_exists"],
                left["after_hash"],
                left["after_mode"],
            )
            == (
                right["before_exists"],
                right["before_hash"],
                right["before_mode"],
            )
            for left, right in zip(group, group[1:])
        )
        if not continuous:
            review = _review_entry(group, "discontinuous_history")
            if review is not None:
                merged.append(review)
            continue
        if any(not item.get("snapshot_eligible", False) for item in group):
            review = _review_entry(group, "discontinuous_history")
            if review is not None:
                merged.append(review)
            continue
        result = _review_entry(group, "")
        if result is None:
            continue
        result["snapshot_eligible"] = True
        before_exists = result["before_exists"]
        after_exists = result["after_exists"]
        if not before_exists and not after_exists:
            continue
        if (
            before_exists
            and after_exists
            and result["before_hash"] == result["after_hash"]
            and result["before_mode"] == result["after_mode"]
        ):
            continue
        result["change_kind"] = (
            "created"
            if not before_exists
            else "deleted"
            if not after_exists
            else "modified"
        )
        merged.append(result)
    return merged


class RecoveryCheckpointWriter:
    def __init__(self, store, workspace_root):
        self.store = store
        self.workspace_root = Path(workspace_root)

    def _base_record(self, checkpoint_type, session_id, run_id, turn_id, parent_checkpoint_id):
        checkpoint_id = new_id("ckpt")
        return new_checkpoint_record(
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            parent_checkpoint_id=parent_checkpoint_id,
            workspace_root=str(self.workspace_root),
        )

    def create_turn_checkpoint(
        self,
        session_id,
        run_id,
        turn_id,
        parent_checkpoint_id,
        tool_change_ids,
        verification_evidence=None,
    ):
        record = self._base_record("turn", session_id, run_id, turn_id, parent_checkpoint_id)
        requested_tool_change_ids = list(dict.fromkeys(tool_change_ids or []))
        record["verification_evidence"] = list(verification_evidence or [])
        file_entries = []
        loaded_tool_changes = []
        missing_tool_change_ids = []
        for tool_change_id in requested_tool_change_ids:
            try:
                tool_change = self.store.load_tool_change_record(tool_change_id)
            except (OSError, ValueError):
                missing_tool_change_ids.append(tool_change_id)
                continue
            if (
                tool_change.get("tool_change_id") != tool_change_id
                or tool_change.get("status") not in _TERMINAL_TOOL_STATUSES
            ):
                missing_tool_change_ids.append(tool_change_id)
                continue
            invalid_entries = False
            for entry in tool_change.get("file_entries", []) or []:
                reason = validate_file_entry(entry)
                if reason:
                    invalid_entries = True
                    break
                sources = entry.get("source_tool_change_ids")
                if sources not in (None, [], [tool_change_id]):
                    invalid_entries = True
                    break
            if invalid_entries:
                missing_tool_change_ids.append(tool_change_id)
                continue
            loaded_tool_changes.append(tool_change)
            file_entries.extend(tool_change.get("file_entries", []) or [])
        record["tool_change_ids"] = [item["tool_change_id"] for item in loaded_tool_changes]
        record["missing_tool_change_ids"] = missing_tool_change_ids
        record["file_entries"] = coalesce_file_entries(file_entries)
        if missing_tool_change_ids:
            record["integrity_errors"] = [
                {
                    "reason": "incomplete_tool_change_history",
                    "tool_change_ids": missing_tool_change_ids,
                }
            ]
        record = self.store.create_checkpoint_record(record)
        # 把 checkpoint 反写到 tool change 上，方便反向溯源
        for tool_change in loaded_tool_changes:
            tool_change["checkpoint_id"] = record["checkpoint_id"]
            self.store.write_tool_change_record(tool_change)
        return record

    def create_restore_checkpoint(
        self,
        session_id,
        run_id,
        turn_id,
        parent_checkpoint_id,
        restore_provenance,
        *,
        status="applied",
        owner_id="",
        file_entries=None,
        verification_evidence=None,
    ):
        record = self._base_record("restore", session_id, run_id, turn_id, parent_checkpoint_id)
        record["status"] = status
        record["owner_id"] = owner_id
        record["file_entries"] = list(file_entries or [])
        record["restore_provenance"] = dict(restore_provenance or {})
        record["verification_evidence"] = list(verification_evidence or [])
        # restore 本身不产生新的 file_entries；影响面记录在 restore_provenance 里
        return self.store.create_checkpoint_record(record)


def current_recovery_checkpoint_id(session):
    if not isinstance(session, dict):
        return ""
    recovery = session.get("recovery") or {}
    return str(recovery.get("current_checkpoint_id") or "")


def set_current_recovery_checkpoint_id(session, checkpoint_id):
    if not isinstance(session, dict):
        raise TypeError("session must be a dict")
    recovery = session.setdefault("recovery", {})
    recovery["current_checkpoint_id"] = str(checkpoint_id or "")
    return recovery["current_checkpoint_id"]
