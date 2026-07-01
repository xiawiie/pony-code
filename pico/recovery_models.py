"""可恢复编辑（recoverable editing）里最基础的一层：数据模型。

这里只做两件事：
1. 定义 Checkpoint Record、Tool Change Record 等结构的 schema 版本号与字段默认值。
2. 定义 TraceTimeline 里跟“恢复”相关的事件名常量。

真正的存储、观察、恢复逻辑不在这里。这个模块被广泛依赖，所以严格保持
stdlib-only、零副作用、可离线构造。
"""

from datetime import datetime, timezone
import uuid

CHECKPOINT_RECORD_SCHEMA_VERSION = "checkpoint-record-v1"
TOOL_CHANGE_RECORD_SCHEMA_VERSION = "tool-change-record-v1"
RESTORE_PLAN_SCHEMA_VERSION = "restore-plan-v1"
VERIFICATION_RECORD_SCHEMA_VERSION = "verification-record-v1"

TRACE_RUN_STARTED = "run_started"
TRACE_MODEL_TURN = "model_turn"
TRACE_TOOL_STARTED = "tool_started"
TRACE_TOOL_FINISHED = "tool_finished"
TRACE_TOOL_INTERRUPTED = "tool_interrupted"
TRACE_CHECKPOINT_CREATED = "checkpoint_created"
TRACE_RESTORE_PREVIEWED = "restore_previewed"
TRACE_RESTORE_APPLIED = "restore_applied"
TRACE_VERIFICATION_RECORDED = "verification_recorded"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_checkpoint_record(checkpoint_id, checkpoint_type, session_id, run_id, turn_id, parent_checkpoint_id, workspace_root):
    return {
        "schema_version": CHECKPOINT_RECORD_SCHEMA_VERSION,
        "checkpoint_id": str(checkpoint_id),
        "checkpoint_type": str(checkpoint_type),
        "created_at": utc_now(),
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "turn_id": str(turn_id or ""),
        "parent_checkpoint_id": str(parent_checkpoint_id or ""),
        "workspace_root": str(workspace_root),
        "tool_change_ids": [],
        "git_review_context": {},
        "file_entries": [],
        "verification_evidence": [],
        "restore_provenance": {},
    }


def new_tool_change_record(tool_change_id, checkpoint_id, turn_id, tool_name, effect_class):
    return {
        "schema_version": TOOL_CHANGE_RECORD_SCHEMA_VERSION,
        "tool_change_id": str(tool_change_id),
        "checkpoint_id": str(checkpoint_id or ""),
        "turn_id": str(turn_id or ""),
        "tool_name": str(tool_name),
        "effect_class": str(effect_class),
        "status": "pending",
        "started_at": utc_now(),
        "ended_at": "",
        "input_summary": {},
        "affected_paths": [],
        "file_entries": [],
        "shell_side_effects": [],
        "approval": {},
        "error": {},
        "trace_event_ids": [],
    }
