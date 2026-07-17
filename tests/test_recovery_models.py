from pico.recovery.models import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_RECORD_TYPE,
    TOOL_CHANGE_FORMAT_VERSION,
    TOOL_CHANGE_RECORD_TYPE,
    TRACE_CHECKPOINT_CREATED,
    TRACE_MODEL_TURN,
    TRACE_RECOVERY_CHECKPOINT_CREATED,
    new_checkpoint_record,
    new_tool_change_record,
)


def test_checkpoint_record_builder_has_phase1_shape():
    record = new_checkpoint_record(
        checkpoint_id="ckpt_1",
        checkpoint_type="turn",
        session_id="session_1",
        run_id="run_1",
        turn_id="task_1",
        parent_checkpoint_id="ckpt_0",
        workspace_root="/repo",
    )

    assert record["record_type"] == CHECKPOINT_RECORD_TYPE
    assert record["format_version"] == CHECKPOINT_FORMAT_VERSION
    assert "schema_version" not in record
    assert record["checkpoint_id"] == "ckpt_1"
    assert record["checkpoint_type"] == "turn"
    assert record["tool_change_ids"] == []
    assert record["file_entries"] == []
    assert record["verification_evidence"] == []
    assert record["restore_provenance"] == {}


def test_tool_change_record_builder_starts_pending():
    record = new_tool_change_record(
        tool_change_id="tc_1",
        checkpoint_id="",
        turn_id="task_1",
        tool_name="write_file",
        effect_class="workspace_write",
    )

    assert record["record_type"] == TOOL_CHANGE_RECORD_TYPE
    assert record["format_version"] == TOOL_CHANGE_FORMAT_VERSION
    assert "schema_version" not in record
    assert record["status"] == "pending"
    assert record["tool_name"] == "write_file"
    assert record["affected_paths"] == []


def test_trace_event_names_are_phase1_focused():
    assert TRACE_MODEL_TURN == "model_turn"
    assert TRACE_CHECKPOINT_CREATED == "checkpoint_created"
    assert TRACE_RECOVERY_CHECKPOINT_CREATED == "recovery_checkpoint_created"
