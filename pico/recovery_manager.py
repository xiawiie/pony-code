"""RecoveryManager 骨架：负责基于 Checkpoint Record 做恢复预览和应用。

Phase 1 只支持“回到某个 turn 之前的原始字节状态”这一种恢复。核心不变式：
    - 只对 file_entries 里 snapshot_eligible=True 的条目动手；
    - 应用前先算当前 sha256，与 expected_current_hash 不符就打成 conflict；
    - 应用后必须写一份 checkpoint_type="restore" 的 Checkpoint Record。
"""

from pathlib import Path
import tempfile

from pico.recovery_models import (
    CHECKPOINT_RECORD_SCHEMA_VERSION,
    RESTORE_PLAN_SCHEMA_VERSION,
    new_id,
    utc_now,
)
from pico.recovery_paths import hash_bytes, hash_file_bytes, resolve_workspace_relative_path


class RecoveryManager:
    def __init__(self, store, workspace_root, checkpoint_writer=None):
        self.store = store
        self.workspace_root = Path(workspace_root)
        self._checkpoint_writer = checkpoint_writer

    # 允许 Pico 在构造完成之后再把 writer 注入进来，避免循环依赖
    def bind_checkpoint_writer(self, writer):
        self._checkpoint_writer = writer

    def preview_restore(self, checkpoint_id):
        record = self.store.load_checkpoint_record(checkpoint_id)
        if record.get("schema_version") != CHECKPOINT_RECORD_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema: " + str(record.get("schema_version")))

        entries = []
        for file_entry in record.get("file_entries", []) or []:
            decision, detail = self._plan_entry(file_entry)
            entries.append({
                "path": file_entry.get("path", ""),
                "decision": decision,
                "reason": detail.get("reason", ""),
                "restore_available": decision == "restore",
                "captured_before_state": bool(file_entry.get("before_blob_ref", "")),
                "recovery_note": detail.get("recovery_note", ""),
                "expected_current_hash": file_entry.get("expected_current_hash", ""),
                "observed_current_hash": detail.get("observed_current_hash", ""),
                "before_blob_ref": file_entry.get("before_blob_ref", ""),
                "after_blob_ref": file_entry.get("after_blob_ref", ""),
                "snapshot_eligible": bool(file_entry.get("snapshot_eligible", False)),
                "ineligible_reason": file_entry.get("ineligible_reason", ""),
                "change_kind": file_entry.get("change_kind", ""),
            })

        return {
            "schema_version": RESTORE_PLAN_SCHEMA_VERSION,
            "restore_plan_id": new_id("plan"),
            "checkpoint_id": checkpoint_id,
            "created_at": utc_now(),
            "entries": entries,
        }

    def _plan_entry(self, file_entry):
        if not file_entry.get("snapshot_eligible", False):
            reason = file_entry.get("ineligible_reason", "not_snapshot_eligible")
            if file_entry.get("before_blob_ref", ""):
                note = f"review required: {reason}; automatic restore is disabled for this entry"
            else:
                note = f"review required: {reason}; no restorable before-state snapshot was captured"
            return "review", {"reason": reason, "recovery_note": note}
        change_kind = file_entry.get("change_kind", "")
        if change_kind in {"modified", "deleted"} and not file_entry.get("before_blob_ref", ""):
            return "review", {"reason": "before_blob_unavailable", "observed_current_hash": ""}
        path = file_entry.get("path", "")
        try:
            resolved = resolve_workspace_relative_path(self.workspace_root, path)
        except ValueError as exc:
            return "review", {"reason": "unresolvable_path", "detail": str(exc)}

        expected = file_entry.get("expected_current_hash", "")
        observed = ""
        if resolved.exists():
            observed = hash_file_bytes(resolved)["content_hash"]

        if not expected:
            if observed:
                return "conflict", {"reason": "unexpected_file_present", "observed_current_hash": observed}
            if not file_entry.get("before_blob_ref", ""):
                return "review", {"reason": "missing_expected_current_hash", "observed_current_hash": ""}
            return "restore", {"reason": "hash_match", "observed_current_hash": ""}
        if expected and observed and expected != observed:
            return "conflict", {"reason": "hash_mismatch", "observed_current_hash": observed}
        if expected and not observed:
            # 期望存在但当前不在，通常是用户已经删了 → 需要人工确认
            return "conflict", {"reason": "file_missing", "observed_current_hash": ""}
        return "restore", {"reason": "hash_match", "observed_current_hash": observed}

    def apply_restore(self, checkpoint_id):
        plan = self.preview_restore(checkpoint_id)
        checkpoint = self.store.load_checkpoint_record(checkpoint_id)
        pre_states = []
        post_states = []
        touched = []
        skipped = []

        for entry in plan["entries"]:
            if entry["decision"] != "restore":
                continue
            path = entry["path"]
            resolved = resolve_workspace_relative_path(self.workspace_root, path)

            before_blob_ref = entry.get("before_blob_ref") or ""
            # 在真正动磁盘之前，先确认 before-blob 还在。如果丢了，把这条 entry
            # 转成 skipped/blob_missing 并继续处理下一条，避免留下半恢复状态。
            if before_blob_ref and not self.store.has_blob(before_blob_ref):
                skipped.append({
                    "path": path,
                    "reason": "before_blob_missing",
                    "before_blob_ref": before_blob_ref,
                })
                continue

            pre_hash = ""
            pre_blob_ref = ""
            if resolved.exists():
                data = resolved.read_bytes()
                pre_info = self.store.write_blob(data, "text")
                pre_hash = pre_info["content_hash"]
                pre_blob_ref = pre_info["blob_ref"]
            pre_states.append({
                "path": path,
                "before_blob_ref": pre_blob_ref,
                "before_hash": pre_hash,
            })

            if before_blob_ref:
                try:
                    data = self.store.read_blob(before_blob_ref)
                except FileNotFoundError:
                    skipped.append({
                        "path": path,
                        "reason": "before_blob_missing",
                        "before_blob_ref": before_blob_ref,
                    })
                    continue
                resolved.parent.mkdir(parents=True, exist_ok=True)
                expected_hash = hash_bytes(data)["content_hash"]
                write_result = _write_bytes_verified(resolved, data, expected_hash)
                if write_result["status"] != "ok":
                    skipped.append({
                        "path": path,
                        "reason": write_result["reason"],
                        "expected_hash": expected_hash,
                        "observed_hash": write_result["observed_hash"],
                        "target_modified": bool(write_result.get("target_modified", False)),
                    })
                    continue
                post_hash = write_result["observed_hash"]
            else:
                # before_blob_ref 为空 → 说明目标状态是“不存在”
                if resolved.exists():
                    resolved.unlink()
                post_hash = ""

            post_states.append({
                "path": path,
                "after_blob_ref": before_blob_ref,
                "after_hash": post_hash,
            })
            touched.append(path)

        provenance = {
            "source_checkpoint_id": checkpoint_id,
            "plan_id": plan["restore_plan_id"],
            "applied_at": utc_now(),
            "restored_paths": touched,
            "skipped_entries": skipped,
            "pre_restore_file_states": pre_states,
            "post_restore_file_states": post_states,
        }

        writer = self._checkpoint_writer or RecoveryCheckpointWriterProxy(self.store, self.workspace_root)
        restore_checkpoint = writer.create_restore_checkpoint(
            session_id=checkpoint.get("session_id", ""),
            run_id=checkpoint.get("run_id", ""),
            turn_id=checkpoint.get("turn_id", ""),
            parent_checkpoint_id=checkpoint_id,
            restore_provenance=provenance,
        )
        return {
            "restore_checkpoint_id": restore_checkpoint["checkpoint_id"],
            "restore_plan_id": plan["restore_plan_id"],
            "restored_paths": touched,
            "skipped_entries": skipped,
        }


class RecoveryCheckpointWriterProxy:
    """在没有真实 writer 注入的情况下，走同样的 store 写法。"""

    def __init__(self, store, workspace_root):
        from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter

        self._writer = RecoveryCheckpointWriter(store, workspace_root)

    def create_restore_checkpoint(self, **kwargs):
        return self._writer.create_restore_checkpoint(**kwargs)


def _write_bytes_verified(path, data, expected_hash):
    """Write to a sibling temp file, verify its bytes, then atomically replace target.

    Invariant: `path` is only touched after the temp file's hash equals
    `expected_hash`. If any check fails we clean up the temp and return
    `status='error'` without mutating the target — half-restore is impossible.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent), prefix=path.name + ".restore.", suffix=".tmp") as handle:
        temp_path = Path(handle.name)
    temp_survived = True
    try:
        temp_path.write_bytes(data)
        temp_hash = hash_file_bytes(temp_path)["content_hash"]
        if temp_hash != expected_hash:
            return {
                "status": "error",
                "reason": "post_write_hash_mismatch",
                "observed_hash": temp_hash,
            }
        temp_path.replace(path)
        temp_survived = False  # replace 之后 temp 已经不在原路径了
        observed_hash = hash_file_bytes(path)["content_hash"]
        if observed_hash != expected_hash:
            # 极小概率的读回不一致（例如底层文件系统 sync 异常）。目标已经写入，
            # 明确标记为 verified_replaced_but_reread_mismatch，避免后续误认为
            # “未改动”。恢复日志需要知道磁盘状态与预期不一致。
            return {
                "status": "error",
                "reason": "reread_hash_mismatch_after_replace",
                "observed_hash": observed_hash,
                "target_modified": True,
            }
        return {"status": "ok", "reason": "", "observed_hash": observed_hash}
    finally:
        if temp_survived:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
