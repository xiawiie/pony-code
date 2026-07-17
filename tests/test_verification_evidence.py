import pony.agent.verification as verification


def test_verification_record_captures_command_level_evidence():
    record = verification.new_verification_record(
        argv=("python", "-m", "pytest", "-q"),
        risk_class="workspace_write",
        runner_executed=True,
        execution_mode="argv",
        exit_code=1,
        stdout="x" * 5000,
        stderr="failed",
        affected_checkpoint_id="ckpt_1",
        trace_event_id="trace_1",
    )

    assert record["status"] == "failed"
    assert record["command"] == "python -m pytest -q"
    assert len(record["stdout_tail"]) <= 1000
    assert record["affected_checkpoint_id"] == "ckpt_1"


def test_verification_record_status_comes_only_from_exit_code():
    failed = verification.new_verification_record(
        argv=("pytest", "-q"),
        risk_class="external_effect",
        runner_executed=True,
        execution_mode="argv",
        exit_code=1,
        stdout="100 passed",
        stderr="",
    )
    passed = verification.new_verification_record(
        argv=("pytest", "-q"),
        risk_class="external_effect",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout="FAILED text is not status",
        stderr="",
    )

    assert failed["status"] == "failed"
    assert passed["status"] == "passed"
