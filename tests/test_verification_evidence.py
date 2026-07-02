from pico.verification import is_verification_command, new_verification_record


def test_verification_record_captures_command_level_evidence():
    record = new_verification_record(
        command="python -m pytest -q",
        risk_class="workspace_write",
        exit_code=1,
        stdout="x" * 5000,
        stderr="failed",
        affected_checkpoint_id="ckpt_1",
        trace_event_id="trace_1",
    )

    assert record["status"] == "failed"
    assert len(record["stdout_tail"]) <= 1000
    assert record["affected_checkpoint_id"] == "ckpt_1"


def test_verification_command_detection_avoids_plain_text_search_false_positive():
    assert is_verification_command("python -m pytest -q") is True
    assert is_verification_command("uv run ruff check .") is True
    assert is_verification_command("rg pytest tests") is False
