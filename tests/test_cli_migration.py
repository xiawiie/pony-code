import json
import subprocess

import pytest

import pico.cli_migration as cli_migration_module
from pico.cli import main
from pico.cli_errors import CLI_EXIT_RUNTIME
from pico.migration import Migration
from pico.observability import load_run_summary, validate_report, validate_trace


def _write_legacy_run(root, run_id="run_1"):
    run_dir = root / ".pico" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"run_id": run_id, "task_id": "task_1"}),
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "task_state.json").write_text("{}", encoding="utf-8")
    return run_dir


def test_migrate_status_separates_transaction_and_live_schema(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write_legacy_run(tmp_path)

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "migrate",
            "observability",
            "status",
        ]
    )

    assert code == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["contract"] == "observability"
    assert data["transaction_state"] == "absent"
    assert data["live_schema_state"] == "migration_required"
    assert "state" not in data


def test_migrate_status_reports_both_states_absent_without_project_state(
    tmp_path,
    capsys,
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "migrate",
            "observability",
            "status",
        ]
    )

    assert code == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data == {
        "contract": "observability",
        "live_schema_state": "absent",
        "transaction_state": "absent",
    }


def test_runs_summary_names_exact_observability_migration_command(
    tmp_path,
    capsys,
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write_legacy_run(tmp_path)

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "runs",
            "summary",
            "run_1",
        ]
    )

    assert code == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["summary_status"] == "migration_required"
    assert data["migration_command"] == "pico migrate observability apply"


def test_migrate_observability_apply_cuts_over_legacy_run(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    legacy = {
        "run_id": "run_1",
        "task_id": "task_1",
        "status": "completed",
        "stop_reason": "final_answer_returned",
        "final_answer": "must not be copied",
        "attempts": 1,
        "tool_steps": 0,
        "completion_usage_totals": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        "last_request_metadata": {},
    }
    (run_dir / "report.json").write_text(json.dumps(legacy), encoding="utf-8")
    events = [
        {"event": "run_started", "created_at": "now"},
        {"event": "run_finished", "created_at": "now", "final_answer": "secret"},
    ]
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    (run_dir / "task_state.json").write_text(
        json.dumps({
            "run_id": "run_1",
            "task_id": "task_1",
            "user_request": "inspect",
            "status": "completed",
            "last_tool": "",
            "stop_reason": "final_answer_returned",
            "final_answer": "done",
            "attempts": 1,
            "tool_steps": 0,
            "checkpoint_id": "",
            "resume_status": "",
            "recovery_checkpoint_id": "",
        }),
        encoding="utf-8",
    )

    assert main(["--cwd", str(tmp_path), "--format", "json", "migrate", "observability", "apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
    validate_report(report, run_id="run_1")
    validate_trace(trace, run_id="run_1", task_id="task_1")
    assert load_run_summary(tmp_path / ".pico" / "runs", "run_1") == report
    assert "final_answer" not in report
    assert "must not be copied" not in json.dumps(report)
    assert all("secret" not in json.dumps(event) for event in trace)


@pytest.mark.parametrize("artifact", ("report", "event", "task_state"))
def test_migrate_observability_rejects_duplicate_json_keys_before_rename(
    tmp_path, monkeypatch, capsys, artifact
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    legacy = {
        "run_id": "run_1",
        "task_id": "task_1",
        "status": "completed",
        "stop_reason": "final_answer_returned",
        "attempts": 1,
        "tool_steps": 0,
        "completion_usage_totals": {},
        "last_request_metadata": {},
    }
    report_text = json.dumps(legacy)
    trace_text = json.dumps({"event": "run_started", "created_at": "now"}) + "\n"
    task_state_text = json.dumps({"run_id": "run_1", "task_id": "task_1"})
    if artifact == "report":
        report_text = '{"run_id":"run_1","run_id":"other"}'
    elif artifact == "event":
        trace_text = '{"event":"run_started","event":"run_finished"}\n'
    else:
        task_state_text = '{"run_id":"run_1","task_id":"task_1","task_id":"other"}'
    (run_dir / "report.json").write_text(report_text, encoding="utf-8")
    (run_dir / "trace.jsonl").write_text(trace_text, encoding="utf-8")
    (run_dir / "task_state.json").write_text(task_state_text, encoding="utf-8")

    monkeypatch.setattr(
        Migration,
        "_rename",
        lambda *args: pytest.fail("duplicate JSON reached migration rename"),
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "migrate",
            "observability",
            "apply",
        ]
    )

    assert code == CLI_EXIT_RUNTIME
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "migration_failed"
    assert "duplicate" in payload["error"]["details"]["reason_code"]


@pytest.mark.parametrize("artifact", ("report.json", "trace.jsonl", "task_state.json"))
def test_migrate_observability_rejects_oversized_artifact_before_rename(
    tmp_path,
    monkeypatch,
    artifact,
):
    import pico.cli_migration as cli_migration

    source = tmp_path / "source"
    run_dir = source / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text("{}", encoding="utf-8")
    (run_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "task_state.json").write_text("{}", encoding="utf-8")
    (run_dir / artifact).write_bytes(b"x" * 9)
    monkeypatch.setattr(cli_migration, "MAX_RUN_ARTIFACT_BYTES", 8)
    monkeypatch.setattr(
        Migration,
        "_rename",
        lambda *args: pytest.fail("oversized artifact reached migration rename"),
    )

    with pytest.raises(ValueError, match="private file too large"):
        cli_migration._build_observability(source, tmp_path / "candidate")


def test_migrate_observability_rejects_oversized_converted_trace(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run_dir = source / "run_1"
    run_dir.mkdir(parents=True)
    report = {
        "run_id": "run_1",
        "task_id": "task_1",
        "status": "completed",
        "stop_reason": "final_answer_returned",
        "attempts": 1,
        "tool_steps": 0,
    }
    task_state = {
        "run_id": "run_1",
        "task_id": "task_1",
        "user_request": "inspect",
        "status": "completed",
        "tool_steps": 0,
        "attempts": 1,
        "last_tool": "",
        "stop_reason": "final_answer_returned",
        "final_answer": "done",
        "checkpoint_id": "",
        "resume_status": "",
        "recovery_checkpoint_id": "",
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "trace.jsonl").write_text(
        json.dumps({"event": "run_finished", "created_at": "now"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "task_state.json").write_text(
        json.dumps(task_state),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_migration_module, "MAX_RUN_ARTIFACT_BYTES", 256)

    with pytest.raises(ValueError, match="private file too large"):
        cli_migration_module._build_observability(
            source,
            tmp_path / "candidate",
        )
