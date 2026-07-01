"""从真实的 Tool Change Record 汇成 Checkpoint Record。

Phase 1 我们只需要两种 checkpoint：
    turn      —— 一次模型 turn 结束后写下的“这次 turn 改了什么”
    restore   —— apply_restore 之后写下的“恢复前后 workspace 长什么样”

会话上下文里“当前 checkpoint”不落在 checkpoint 记录本身，而是挂在 session 字典的
`recovery.current_checkpoint_id` 上。用两个辅助函数读写。
"""

from pathlib import Path

from pico.recovery_models import new_checkpoint_record, new_id


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
        requested_tool_change_ids = list(tool_change_ids or [])
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
            loaded_tool_changes.append(tool_change)
            file_entries.extend(tool_change.get("file_entries", []) or [])
        record["tool_change_ids"] = [item["tool_change_id"] for item in loaded_tool_changes]
        record["missing_tool_change_ids"] = missing_tool_change_ids
        record["file_entries"] = file_entries
        self.store.write_checkpoint_record(record)
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
        verification_evidence=None,
    ):
        record = self._base_record("restore", session_id, run_id, turn_id, parent_checkpoint_id)
        record["restore_provenance"] = dict(restore_provenance or {})
        record["verification_evidence"] = list(verification_evidence or [])
        # restore 本身不产生新的 file_entries；影响面记录在 restore_provenance 里
        self.store.write_checkpoint_record(record)
        return record


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
