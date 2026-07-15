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
    def __init__(self, store, owner_id=""):
        self.store = store
        self.owner_id = str(owner_id or "")

    def start(
        self,
        checkpoint_id,
        turn_id,
        tool_name,
        effect_class,
        input_summary,
        *,
        policy=None,
        sandbox=None,
        prepared_file_entries=None,
        recovery_context=None,
    ):
        tool_change_id = new_id("tc")
        record = new_tool_change_record(
            tool_change_id=tool_change_id,
            checkpoint_id=checkpoint_id or "",
            turn_id=turn_id or "",
            tool_name=tool_name,
            effect_class=effect_class,
            owner_id=self.owner_id,
        )
        record["input_summary"] = dict(input_summary or {})
        record["policy"] = dict(policy or {})
        record["sandbox"] = dict(sandbox or {})
        record["prepared_file_entries"] = list(
            prepared_file_entries or []
        )
        record["recovery_context"] = dict(recovery_context or {})
        return self.store.create_tool_change_record(record)

    def finalize(
        self,
        tool_change_id,
        status,
        affected_paths=None,
        file_entries=None,
        error=None,
        shell_side_effects=None,
        approval=None,
        sandbox=None,
        trace_event_ids=None,
        checkpoint_id=None,
    ):
        if status not in _ALLOWED_TERMINAL_STATUSES:
            raise ValueError("unsupported terminal status: " + str(status))
        def transform(record):
            if record.get("owner_id", "") != self.owner_id:
                raise ValueError("owner_mismatch")
            if record.get("status") != "pending":
                raise ValueError("status_conflict")
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
            if sandbox is not None:
                record["sandbox"] = dict(sandbox)
            if trace_event_ids is not None:
                record["trace_event_ids"] = list(trace_event_ids)
            if checkpoint_id is not None:
                record["checkpoint_id"] = str(checkpoint_id)
            return record

        return self.store.update_tool_change_record(
            tool_change_id,
            transform,
            expected_status="pending",
        )

    def pending_recovery_reviews(self):
        return [
            record
            for record in self.store.list_tool_change_records(strict=True)
            if record.get("status") == "pending"
            or (
                record.get("status") in {"interrupted", "partial_success"}
                and not record.get("reviewed_at")
            )
        ]

    def resolve_pending(
        self,
        tool_change_id,
        *,
        reviewed_by,
        review_reason,
        expected_record_hash=None,
    ):
        def transform(record):
            status = record.get("status")
            if status == "pending":
                record["status"] = "interrupted"
                record["ended_at"] = utc_now()
            elif status not in {"interrupted", "partial_success"} or record.get(
                "reviewed_at"
            ):
                raise ValueError("status_conflict")
            record["reviewed_at"] = utc_now()
            record["reviewed_by"] = str(reviewed_by)
            record["review_reason"] = str(review_reason)
            return record

        with self.store.mutation_lock():
            if expected_record_hash is not None:
                return self.store.update_tool_change_record_if_hash(
                    tool_change_id,
                    expected_record_hash,
                    transform,
                )
            return self.store.update_tool_change_record(
                tool_change_id,
                transform,
            )

    def mark_interrupted_pending(self, legacy_only=False):
        """把所有仍是 pending 的记录改成 interrupted，返回被改动的记录列表。

        通常在会话重启/异常兜底时调用。已经 terminal 的记录不动。
        """
        touched = []
        for record in self.pending_recovery_reviews():
            if record.get("status") != "pending":
                continue
            if legacy_only and record.get("owner_id"):
                continue
            if record.get("owner_id", "") != self.owner_id:
                continue
            updated = self.finalize(
                record["tool_change_id"],
                status="interrupted",
            )
            touched.append(updated)
        return touched
