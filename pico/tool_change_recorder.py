"""Tool Change Record 的生命周期管理。

约定：
    start()                         →  写一条 status=pending 的记录，返回记录 dict
    finalize(id, status, ...)       →  更新 status/ended_at/affected_paths/file_entries/error
    mark_interrupted_pending()      →  把所有仍在 pending 的记录改成 interrupted

“pending 记录必须闭环”是 Phase 1 的硬不变式，任何 tool 执行路径退出前都要走 finalize。
"""

from pico.recovery_models import (
    new_id,
    new_tool_change_record,
    utc_now,
)


_ALLOWED_TERMINAL_STATUSES = {"finalized", "error", "partial_success", "interrupted"}


class ToolChangeRecorder:
    def __init__(self, store):
        self.store = store

    def start(self, checkpoint_id, turn_id, tool_name, effect_class, input_summary):
        tool_change_id = new_id("tc")
        record = new_tool_change_record(
            tool_change_id=tool_change_id,
            checkpoint_id=checkpoint_id or "",
            turn_id=turn_id or "",
            tool_name=tool_name,
            effect_class=effect_class,
        )
        record["input_summary"] = dict(input_summary or {})
        self.store.write_tool_change_record(record)
        return record

    def finalize(
        self,
        tool_change_id,
        status,
        affected_paths=None,
        file_entries=None,
        error=None,
        shell_side_effects=None,
        approval=None,
        trace_event_ids=None,
        checkpoint_id=None,
    ):
        if status not in _ALLOWED_TERMINAL_STATUSES:
            raise ValueError("unsupported terminal status: " + str(status))
        record = self.store.load_tool_change_record(tool_change_id)
        record["status"] = status
        record["ended_at"] = utc_now()
        if affected_paths is not None:
            record["affected_paths"] = list(affected_paths)
        if file_entries is not None:
            record["file_entries"] = list(file_entries)
        if error is not None:
            record["error"] = dict(error)
        if shell_side_effects is not None:
            record["shell_side_effects"] = list(shell_side_effects)
        if approval is not None:
            record["approval"] = dict(approval)
        if trace_event_ids is not None:
            record["trace_event_ids"] = list(trace_event_ids)
        if checkpoint_id is not None:
            record["checkpoint_id"] = str(checkpoint_id)
        self.store.write_tool_change_record(record)
        return record

    def mark_interrupted_pending(self):
        """把所有仍是 pending 的记录改成 interrupted，返回被改动的记录列表。

        通常在会话重启/异常兜底时调用。已经 terminal 的记录不动。
        """
        touched = []
        for record in self.store.list_tool_change_records():
            if record.get("status") != "pending":
                continue
            updated = self.finalize(
                record["tool_change_id"],
                status="interrupted",
            )
            touched.append(updated)
        return touched
