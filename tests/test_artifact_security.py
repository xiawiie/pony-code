import json
import os
from pathlib import Path
import stat

import pytest

from pony.security import private_files as security_module
from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pony.state.checkpoint_store import CheckpointStore
from pony.cli.app import main
from pony.cli.session import inspect_session
from pony.memory.block_store import BlockStore
from pony.recovery.models import new_checkpoint_record, new_tool_change_record
from pony.state.run_store import RunStore
from pony.state.session_store import (
    LEGACY_SESSION_FORMAT_VERSION,
    SESSION_FORMAT_VERSION,
)
from pony.state.task_state import TaskState
from pony.runtime.options import RuntimeOptions


def _build_agent(root, *, secret_env_names=()):
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pony" / "sessions"),
        options=RuntimeOptions(
            approval_policy="auto", secret_env_names=secret_env_names
        ),
    )


def _assert_mode(path, expected):
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == expected


def _verification(stdout):
    return {
        "verification_id": "verify_test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "argv": ["pytest"],
        "runner_executed": True,
        "execution_mode": "argv",
        "command": "pytest",
        "risk_class": "read_only",
        "exit_code": 0,
        "status": "passed",
        "stdout_tail": stdout,
        "stderr_tail": "",
        "affected_checkpoint_id": "",
        "trace_event_id": "",
    }


def _session(session_id, workspace_root="/repo"):
    return {
        "record_type": "session",
        "format_version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(workspace_root),
        "messages": [],
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }


def test_secret_canary_is_absent_from_normal_artifacts_and_inspection(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "github_pat_A123456789012345678901234567890"
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)
    agent = _build_agent(tmp_path, secret_env_names=("PONY_TEST_TOKEN",))
    state = TaskState.create(
        run_id="run_canary",
        task_id="task_canary",
        user_request=secret,
    )
    agent.run_store.start_run(state)
    agent.emit_trace(state, "canary", {"token": secret})
    agent.run_store.write_report(state, {"token": secret})
    change = agent.tool_change_recorder.start(
        "",
        state.task_id,
        "run_shell",
        "workspace_write",
        {"command": secret},
    )
    agent.tool_change_recorder.finalize(
        change["tool_change_id"],
        "error",
        error={"message": secret},
    )
    record = new_checkpoint_record(
        "ckpt_canary",
        "turn",
        "s",
        state.run_id,
        state.task_id,
        "",
        str(tmp_path),
    )
    record["verification_evidence"] = [_verification(secret)]
    agent.checkpoint_store.write_checkpoint_record(record)

    for path in (tmp_path / ".pony").rglob("*"):
        if (
            path.is_file()
            and "/sessions/backup/" not in path.as_posix()
            and "/blobs/" not in path.as_posix()
        ):
            assert secret.encode() not in path.read_bytes(), path

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "show",
        "ckpt_canary",
            ]
        )
        == 0
    )
    assert secret not in capsys.readouterr().out


def test_checkpoint_json_is_redacted_but_blob_bytes_remain_exact_and_private(
    tmp_path,
):
    secret = "opaque-checkpoint-secret-123456789"

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(secret, "<redacted>")
        return value

    store = CheckpointStore(tmp_path, redactor=redact)
    blob = store.write_blob(secret.encode(), "text")
    record = new_checkpoint_record(
        "ckpt_private",
        "turn",
        "s",
        "r",
        "t",
        "",
        str(tmp_path),
    )
    record["verification_evidence"] = [_verification(secret)]
    record_path = store.write_checkpoint_record(record)

    assert secret not in record_path.read_text(encoding="utf-8")
    assert store.read_blob(blob["blob_ref"]) == secret.encode()
    for directory in (
        store.root,
        store.records_dir,
        store.tool_changes_dir,
        store.blobs_dir,
        store._blob_path(blob["blob_ref"]).parent,
    ):
        _assert_mode(directory, 0o700)
    for path in (
        record_path,
        store._blob_path(blob["blob_ref"]),
        store.lock_path,
    ):
        _assert_mode(path, 0o600)


def test_checkpoint_mutation_rejects_unsafe_record_and_store_directory(tmp_path):
    store = CheckpointStore(tmp_path)
    outside_file = tmp_path / "outside.json"
    outside_file.write_text("outside\n", encoding="utf-8")
    record_path = store._record_path("ckpt_link")
    record_path.symlink_to(outside_file)
    record = new_checkpoint_record(
        "ckpt_link",
        "turn",
        "s",
        "r",
        "t",
        "",
        str(tmp_path),
    )

    with pytest.raises(ValueError, match="symlink|regular|unsafe"):
        store.write_checkpoint_record(record)
    assert outside_file.read_text(encoding="utf-8") == "outside\n"

    record_path.unlink()
    store.records_dir.rmdir()
    outside_dir = tmp_path / "outside-records"
    outside_dir.mkdir()
    store.records_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|regular|unsafe"):
        store.write_checkpoint_record(record)
    assert list(outside_dir.iterdir()) == []


def test_checkpoint_rejects_fifo_leaf_and_symlinked_blob_bucket(tmp_path):
    store = CheckpointStore(tmp_path)
    fifo = store._tool_change_path("tc_fifo")
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular|unsafe"):
        store.write_tool_change_record(
            new_tool_change_record("tc_fifo", "", "t", "write_file", "workspace_write")
        )

    data = b"exact blob bytes"
    from pony.recovery.paths import hash_bytes

    blob_ref = hash_bytes(data)["content_hash"]
    outside = tmp_path / "outside-blobs"
    outside.mkdir()
    (store.blobs_dir / blob_ref[:2]).symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="symlink|unsafe"):
        store.write_blob(data)
    assert list(outside.iterdir()) == []


def test_checkpoint_temp_swap_preserves_unknown_installed_symlink(
    tmp_path,
    monkeypatch,
):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-temp.json"
    outside.write_text("outside\n", encoding="utf-8")
    from pony.security import private_files as security_module

    original_replace = security_module.os.replace

    def swap_before_replace(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        if str(source).endswith(".tmp"):
            os.unlink(source, dir_fd=src_dir_fd)
            os.symlink(outside, source, dir_fd=src_dir_fd)
        return original_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(security_module.os, "replace", swap_before_replace)
    record = new_checkpoint_record(
        "ckpt_swap",
        "turn",
        "s",
        "r",
        "t",
        "",
        str(tmp_path),
    )

    with pytest.raises(ValueError, match="temp|changed|regular|symlink"):
        store.write_checkpoint_record(record)

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert store._record_path("ckpt_swap").is_symlink()


def test_legacy_inspection_redacts_process_and_project_collision(
    tmp_path,
    monkeypatch,
    capsys,
):
    process_secret = "opaque-process-secret-123456789"
    project_secret = "opaque-project-secret-987654321"
    collision_secret = "opaque-existing-collision-246813579"
    monkeypatch.setenv("PONY_TEST_TOKEN", process_secret)
    monkeypatch.setenv("PONY_REDACTION_COLLISION_0_SECRET", collision_secret)
    (tmp_path / ".env").write_text(
        f"PONY_TEST_TOKEN={project_secret}\n",
        encoding="utf-8",
    )
    legacy_text = f"{process_secret} {project_secret} {collision_secret}"

    sessions = tmp_path / ".pony" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "legacy.json").write_text(
        json.dumps({"id": "legacy", "message": legacy_text}),
        encoding="utf-8",
    )
    runs = tmp_path / ".pony" / "runs" / "legacy"
    runs.mkdir(parents=True)
    (runs / "report.json").write_text(
        json.dumps({"message": legacy_text}),
        encoding="utf-8",
    )
    checkpoints = tmp_path / ".pony" / "checkpoints" / "records"
    checkpoints.mkdir(parents=True)
    checkpoints.parent.chmod(0o700)
    checkpoints.chmod(0o700)
    checkpoint = new_checkpoint_record(
        "ckpt_legacy",
        "turn",
        "s",
        "r",
        "t",
        "",
        str(tmp_path),
    )
    checkpoint["verification_evidence"] = [_verification(legacy_text)]
    checkpoint_path = checkpoints / "ckpt_legacy.json"
    checkpoint_path.write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )
    checkpoint_path.chmod(0o600)

    commands = (
        ["sessions", "show", "legacy"],
        ["runs", "show", "legacy"],
        ["checkpoints", "show", "ckpt_legacy"],
    )
    for command in commands:
        for output_format in ("text", "json"):
            assert (
                main(
                    [
                "--cwd",
                str(tmp_path),
                "--format",
                output_format,
                *command,
                    ]
                )
                == 0
            )
            output = capsys.readouterr().out
            assert process_secret not in output
            assert project_secret not in output
            assert collision_secret not in output
            assert "<redacted>" in output

    for missing in ("tool_changes", "blobs", "quarantine"):
        assert not (checkpoints.parent / missing).exists()


@pytest.mark.parametrize("unsafe_kind", ("mode", "symlink", "owner"))
def test_read_only_checkpoint_inspection_rejects_unsafe_existing_sibling(
    tmp_path,
    monkeypatch,
    unsafe_kind,
):
    root = tmp_path / ".pony" / "checkpoints"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    sibling = root / "tool_changes"
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside-tool-changes"
        outside.mkdir(mode=0o700)
        sibling.symlink_to(outside, target_is_directory=True)
    else:
        sibling.mkdir(mode=0o700)
        sibling.chmod(0o700)
        if unsafe_kind == "mode":
            sibling.chmod(0o755)
        else:
            real_geteuid = os.geteuid
            calls = 0

            def changed_euid_after_root():
                nonlocal calls
                calls += 1
                return real_geteuid() if calls == 1 else sibling.stat().st_uid + 1

            monkeypatch.setattr(os, "geteuid", changed_euid_after_root)

    with pytest.raises(ValueError, match="permissions|unsafe"):
        CheckpointStore(tmp_path, read_only=True)


def test_runs_show_redacts_structured_json_before_rendering(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = 'opaque"quote\\value-123456789'
    monkeypatch.setenv("CUSTOM_OPAQUE", secret)
    run_dir = tmp_path / ".pony" / "runs" / "escaped"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"message": secret}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text(
        json.dumps({"event": "legacy", "message": secret}) + "\n",
        encoding="utf-8",
    )
    escaped = json.dumps(secret)[1:-1]

    for output_format in ("text", "json"):
        assert (
            main(
                [
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_OPAQUE",
            "--format",
            output_format,
            "runs",
            "show",
            "escaped",
                ]
            )
            == 0
        )
        output = capsys.readouterr().out
        assert secret not in output
        assert escaped not in output
        assert "<redacted>" in output


def test_runs_show_rejects_path_escape_and_symlink(tmp_path, capsys):
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir()
    secret = "outside-run-secret"
    (outside_run / "report.json").write_text(secret, encoding="utf-8")
    runs_root = tmp_path / ".pony" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "linked").symlink_to(outside_run, target_is_directory=True)

    for run_id in ("../../outside-run", "linked"):
        code = main(
            [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "runs",
            "show",
            run_id,
            ]
        )
        captured = capsys.readouterr()
        assert code == 2
        assert secret not in captured.out + captured.err


def test_checkpoint_prefix_never_resolves_traversing_record_id(tmp_path, capsys):
    records = tmp_path / ".pony" / "checkpoints" / "records"
    records.mkdir(parents=True)
    malicious_id = "abcdef/../../../outside"
    (records / "entry.json").write_text(
        json.dumps(
            {
            "checkpoint_id": malicious_id,
            "checkpoint_type": "turn",
            "created_at": "2026-07-11T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    outside = tmp_path / ".pony" / "outside.json"
    secret = "outside-checkpoint-secret"
    outside.write_text(json.dumps({"message": secret}), encoding="utf-8")

    code = main(
        [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "show",
        "abcdef",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.out + captured.err


def test_checkpoint_cli_rejects_symlink_record_as_stable_error(tmp_path, capsys):
    records = tmp_path / ".pony" / "checkpoints" / "records"
    records.mkdir(parents=True)
    outside = tmp_path / "outside-checkpoint.json"
    secret = "outside-symlink-checkpoint-secret"
    outside.write_text(json.dumps({"message": secret}), encoding="utf-8")
    (records / "linked.json").symlink_to(outside)

    code = main(
        [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "list",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "unsafe_artifact" in captured.out
    assert secret not in captured.out + captured.err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_runs_show_rejects_fifo_without_opening_it(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / ".pony" / "runs" / "fifo"
    run_dir.mkdir(parents=True)
    fifo = run_dir / "report.json"
    os.mkfifo(fifo)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == fifo:
            raise AssertionError("unsafe FIFO read attempted")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    code = main(
        [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "runs",
        "show",
        "fifo",
        ]
    )

    assert code == 2
    assert "unsafe FIFO read attempted" not in capsys.readouterr().out


def test_run_store_rejects_ids_that_escape_its_root(tmp_path):
    root = tmp_path / ".pony" / "runs"
    store = RunStore(root)
    outside = tmp_path / "outside-run-store"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside\n", encoding="utf-8")

    for run_id in ("../escaped", str(outside)):
        state = TaskState.create(
            run_id=run_id,
            task_id="task",
            user_request="safe",
        )
        with pytest.raises(ValueError, match="invalid run id"):
            store.start_run(state)

    assert list(outside.iterdir()) == [marker]
    assert not (root.parent / "escaped").exists()


def test_nested_runtime_redacts_custom_run_and_session_stores(tmp_path):
    secret = "opaque-nested-secret-123456789"
    run_store = RunStore(tmp_path / ".pony" / "runs")
    session_store = SessionStore(tmp_path / ".pony" / "sessions")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=session_store,
        options=RuntimeOptions(
        run_store=run_store,
        approval_policy="never",
        depth=1,
        redaction_env={"CUSTOM_OPAQUE": secret},
        secret_env_names=("CUSTOM_OPAQUE",),
        ),
    )
    state = TaskState.create(
        run_id="nested",
        task_id="task",
        user_request=secret,
    )

    agent.run_store.start_run(state)
    agent.run_store.append_trace(state, {"message": secret})
    agent.run_store.write_report(state, {"message": secret})

    for path in (
        agent.run_store.task_state_path(state),
        agent.run_store.trace_path(state),
        agent.run_store.report_path(state),
    ):
        assert secret not in path.read_text(encoding="utf-8")


def test_store_constructors_harden_only_their_owned_legacy_tree(tmp_path):
    runs_root = tmp_path / ".pony" / "runs"
    run_dir = runs_root / "legacy"
    run_dir.mkdir(parents=True)
    run_file = run_dir / "report.json"
    run_file.write_text("{}\n", encoding="utf-8")
    sessions_root = tmp_path / ".pony" / "sessions"
    backup_dir = sessions_root / "backup"
    backup_dir.mkdir(parents=True)
    session_file = sessions_root / "legacy.json"
    backup_file = backup_dir / "legacy.v2.json"
    session_file.write_text("{}\n", encoding="utf-8")
    backup_file.write_text("raw\n", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    source.chmod(0o644)

    RunStore(runs_root)
    SessionStore(sessions_root)

    for directory in (runs_root, run_dir, sessions_root, backup_dir):
        _assert_mode(directory, 0o700)
    for path in (run_file, session_file, backup_file):
        _assert_mode(path, 0o600)
    _assert_mode(source, 0o644)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_singular_session_inspection_never_reads_unsafe_paths(
    tmp_path,
    monkeypatch,
):
    sessions_root = tmp_path / ".pony" / "sessions"
    backup = sessions_root / "backup"
    backup.mkdir(parents=True)
    outside_base = tmp_path / "absolute-session"
    outside = outside_base.with_suffix(".json")
    outside.write_text('{"schema_version": 3, "messages": []}\n', encoding="utf-8")
    parent_outside = sessions_root.parent / "parent.json"
    parent_outside.write_text("{}\n", encoding="utf-8")
    backup_file = backup / "raw.json"
    backup_file.write_text("{}\n", encoding="utf-8")
    linked = sessions_root / "linked.json"
    linked.symlink_to(outside)
    fifo = sessions_root / "fifo.json"
    os.mkfifo(fifo)
    unsafe_paths = {outside, parent_outside, backup_file, linked, fifo}
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path in unsafe_paths:
            raise AssertionError("unsafe session read attempted")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    for session_id in (
        str(outside_base),
        "../parent",
        "backup/raw",
        "linked",
        "fifo",
    ):
        ok, report = inspect_session(session_id, sessions_root)
        assert ok is False
        assert "unsafe session read attempted" not in report

    safe = sessions_root / "safe.json"
    (sessions_root / ".session_store.lock").touch(mode=0o600)
    safe.write_text(
        json.dumps(
            {
            "record_type": "session",
                "format_version": LEGACY_SESSION_FORMAT_VERSION,
            "id": "safe",
            "created_at": "2026-01-01T00:00:00+00:00",
            "workspace_root": str(tmp_path),
            "messages": [],
            "working_memory": {},
            "memory": {},
            "recently_recalled": [],
            "checkpoints": {},
            "resume_state": {},
            "recovery": {},
            "runtime_identity": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    safe.chmod(0o600)
    assert inspect_session("safe", sessions_root)[0] is True


def test_singular_session_inspection_does_not_echo_invalid_role(tmp_path):
    sessions_root = tmp_path / ".pony" / "sessions"
    sessions_root.mkdir(parents=True)
    secret = "opaque-invalid-role-secret-123456789"
    (sessions_root / "legacy.json").write_text(
        json.dumps(
            {
            "schema_version": 3,
            "messages": [{"role": secret, "content": "safe"}],
            }
        ),
        encoding="utf-8",
    )

    ok, report = inspect_session("legacy", sessions_root)

    assert ok is False
    assert secret not in report


def test_singular_session_cli_redacts_untrusted_summary_fields(
    tmp_path,
    monkeypatch,
    capsys,
):
    sessions_root = tmp_path / ".pony" / "sessions"
    sessions_root.mkdir(parents=True)
    secret = "opaque-session-summary-secret-123456789"
    monkeypatch.setenv("CUSTOM_SESSION_TOKEN", secret)
    (sessions_root / "legacy.json").write_text(
        json.dumps({"schema_version": secret, "messages": []}),
        encoding="utf-8",
    )

    code = main(
        [
        "--cwd",
        str(tmp_path),
        "--secret-env-name",
        "CUSTOM_SESSION_TOKEN",
        "session",
        "inspect",
        "legacy",
        ]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert secret not in output
    assert "unsafe session artifact" in output


def test_checkpoint_ambiguity_errors_are_redacted(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "opaquecheckpointsecret123456789"
    monkeypatch.setenv("CUSTOM_CHECKPOINT_TOKEN", secret)
    store = CheckpointStore(tmp_path)
    for suffix in ("a", "b"):
        store.write_checkpoint_record(
            new_checkpoint_record(
            f"{secret}{suffix}",
            "turn",
            "s",
            "r",
            "t",
            "",
            str(tmp_path),
            )
        )

    for output_format in ("text", "json"):
        code = main(
            [
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_CHECKPOINT_TOKEN",
            "--format",
            output_format,
            "checkpoints",
            "show",
            secret,
            ]
        )
        captured = capsys.readouterr()
        assert code == 2
        assert secret not in captured.out + captured.err


def test_status_redacts_latest_artifact_ids(tmp_path, monkeypatch, capsys):
    secret = "opaque-status-secret-123456789"
    monkeypatch.setenv("CUSTOM_STATUS_TOKEN", secret)
    (tmp_path / ".env").write_text(
        "PONY_API_BASE=https://api.deepseek.com\n",
        encoding="utf-8",
    )
    (tmp_path / ".pony" / "runs" / secret).mkdir(parents=True)

    for output_format in ("text", "json"):
        code = main(
            [
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_STATUS_TOKEN",
            "--format",
            output_format,
            "status",
            ]
        )
        output = capsys.readouterr().out
        assert code == 0
        assert secret not in output
        assert "<redacted>" in output


def test_status_does_not_follow_symlinked_pony_ancestor(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    (outside / "runs" / "external-run").mkdir(parents=True)
    (workspace / ".pony").symlink_to(outside, target_is_directory=True)

    code = main(
        [
        "--cwd",
        str(workspace),
        "--format",
        "json",
        "status",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["storage"]["runs"] is False
    assert payload["data"]["latest"]["run_id"] is None


def test_config_and_doctor_redact_configured_api_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "opaque-diagnostics-model-secret-123456789"
    monkeypatch.setenv("CUSTOM_DIAGNOSTIC_TOKEN", secret)
    (tmp_path / ".env").write_text(
        f"PONY_API_BASE=https://example.com/{secret}\n",
        encoding="utf-8",
    )

    for command in (("config", "show"), ("doctor",)):
        for output_format in ("text", "json"):
            code = main(
                [
                "--cwd",
                str(tmp_path),
                "--secret-env-name",
                "CUSTOM_DIAGNOSTIC_TOKEN",
                "--format",
                output_format,
                *command,
                ]
            )
            output = capsys.readouterr().out
            assert code == 0
            assert secret not in output


def test_owned_store_constructors_reject_hardlinks_without_chmod(tmp_path):
    cases = []

    run_root = tmp_path / "run-case" / ".pony" / "runs"
    run_file = run_root / "legacy" / "report.json"
    cases.append((run_file, lambda: RunStore(run_root)))

    session_root = tmp_path / "session-case" / ".pony" / "sessions"
    session_file = session_root / "legacy.json"
    cases.append((session_file, lambda: SessionStore(session_root)))

    checkpoint_root = tmp_path / "checkpoint-case"
    checkpoint_file = (
        checkpoint_root / ".pony" / "checkpoints" / "records" / "legacy.json"
    )
    cases.append((checkpoint_file, lambda: CheckpointStore(checkpoint_root)))

    memory_workspace = tmp_path / "memory-case" / "workspace"
    memory_user = tmp_path / "memory-case" / "user"
    memory_file = memory_workspace / "agent_notes.md"
    cases.append(
        (
        memory_file,
        lambda: BlockStore(memory_workspace, memory_user, redaction_env={}),
        )
    )

    for index, (artifact, construct) in enumerate(cases):
        outside = tmp_path / f"outside-hardlink-{index}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        outside.chmod(0o644)
        artifact.parent.mkdir(parents=True)
        os.link(outside, artifact)

        with pytest.raises(ValueError, match="link|private"):
            construct()

        assert outside.read_text(encoding="utf-8") == "outside\n"
        _assert_mode(outside, 0o644)


def test_run_trace_rejects_hardlink_without_touching_external_inode(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(run_id="hardlinked", task_id="task", user_request="safe")
    store.start_run(state)
    outside = tmp_path / "outside-trace.jsonl"
    outside.write_text("outside\n", encoding="utf-8")
    outside.chmod(0o644)
    os.link(outside, store.trace_path(state))

    with pytest.raises(ValueError, match="link|private"):
        store.append_trace(state, {"event": "must_not_land"})

    assert outside.read_text(encoding="utf-8") == "outside\n"
    _assert_mode(outside, 0o644)


def test_session_temp_swap_preserves_unknown_installed_symlink(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    outside = tmp_path / "outside-session.json"
    outside.write_text("outside\n", encoding="utf-8")
    original_replace = security_module.os.replace

    def swap_before_replace(source, target, **kwargs):
        source_dir_fd = kwargs.get("src_dir_fd")
        if str(source).endswith(".tmp"):
            os.unlink(source, dir_fd=source_dir_fd)
            os.symlink(outside, source, dir_fd=source_dir_fd)
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", swap_before_replace)

    with pytest.raises(ValueError, match="temp|changed|regular|symlink"):
        store.save(_session("swapped", tmp_path))

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert store.path("swapped").is_symlink()


def test_store_redactors_cannot_mutate_callers_or_leave_failed_trace(tmp_path):
    def mutating_redactor(value):
        if value.get("record_type") == "session":
            value["recovery"]["mutated"] = True
        else:
            value["mutated"] = True
        return value

    run_store = RunStore(tmp_path / ".pony" / "runs", redactor=mutating_redactor)
    state = TaskState.create(run_id="isolated", task_id="task", user_request="safe")
    run_store.start_run(state)
    event = {"event": "safe"}
    report = {"status": "safe"}
    run_store.append_trace(state, event)
    run_store.write_report(state, report)
    assert event == {"event": "safe"}
    assert report == {"status": "safe"}

    session_store = SessionStore(
        tmp_path / ".pony" / "sessions",
        redactor=mutating_redactor,
    )
    session = _session("isolated", tmp_path)
    session_store.save(session)
    assert session == _session("isolated", tmp_path)

    def failing_redactor(_value):
        raise RuntimeError("redactor failed")

    failing_store = RunStore(
        tmp_path / ".pony" / "failed-runs",
        redactor=failing_redactor,
    )
    failing_state = TaskState.create(
        run_id="failed",
        task_id="task",
        user_request="safe",
    )
    with pytest.raises(RuntimeError, match="redactor failed"):
        failing_store.append_trace(failing_state, {"event": "safe"})
    assert not failing_store.trace_path(failing_state).exists()


def test_private_chmod_rejects_leaf_swapped_to_external_hardlink(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "private.txt"
    target.write_text("private\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    outside.chmod(0o644)
    real_open = security_module.os.open
    swapped = False

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and (
            Path(path) == target
            or (dir_fd is not None and os.fspath(path) == target.name)
        ):
            swapped = True
            target.unlink()
            os.link(outside, target)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "open", swap_before_open)

    with pytest.raises(ValueError, match="link|private|changed"):
        security_module.ensure_private_file(target)

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "outside\n"
    _assert_mode(outside, 0o644)


def test_atomic_writers_remove_installed_temp_with_extra_hardlink(
    tmp_path,
    monkeypatch,
):
    session_store = SessionStore(tmp_path / "session" / ".pony" / "sessions")
    run_store = RunStore(tmp_path / "run" / ".pony" / "runs")
    run_state = TaskState.create(run_id="run", task_id="task", user_request="safe")
    run_store.start_run(run_state)
    checkpoint_store = CheckpointStore(tmp_path / "checkpoint")
    memory_workspace = tmp_path / "memory" / "workspace"
    memory_user = tmp_path / "memory" / "user"
    memory_workspace.mkdir(parents=True)
    memory_user.mkdir(parents=True)
    memory_store = BlockStore(memory_workspace, memory_user, redaction_env={})
    aliases = []
    real_path_replace = Path.replace
    real_os_replace = security_module.os.replace

    def hardlink_before_path_replace(path, target):
        alias = tmp_path / f"temp-alias-{len(aliases)}"
        os.link(path, alias)
        aliases.append(alias)
        return real_path_replace(path, target)

    def hardlink_before_os_replace(source, target, **kwargs):
        alias = tmp_path / f"temp-alias-{len(aliases)}"
        os.link(source, alias, src_dir_fd=kwargs.get("src_dir_fd"))
        aliases.append(alias)
        return real_os_replace(source, target, **kwargs)

    monkeypatch.setattr(Path, "replace", hardlink_before_path_replace)
    monkeypatch.setattr(
        security_module.os,
        "replace",
        hardlink_before_os_replace,
    )

    record = new_checkpoint_record(
        "hardlinked_temp",
        "turn",
        "s",
        "r",
        "t",
        "",
        str(tmp_path),
    )
    cases = (
        (
            lambda: session_store.save(_session("hardlinked_temp", tmp_path)),
            session_store.path("hardlinked_temp"),
        ),
        (
            lambda: run_store.write_report(run_state, {"status": "safe"}),
            run_store.report_path(run_state),
        ),
        (
            lambda: checkpoint_store.write_checkpoint_record(record),
            checkpoint_store._record_path("hardlinked_temp"),
        ),
        (
            lambda: memory_store.append_agent_note("workspace", "safe note"),
            memory_workspace / "agent_notes.md",
        ),
    )

    for write, canonical in cases:
        with pytest.raises(ValueError, match="link|private|temp|changed"):
            write()
        assert not canonical.exists()


@pytest.mark.parametrize("existing", (False, True))
def test_atomic_writer_hardlink_race_restores_previous_target(
    tmp_path,
    monkeypatch,
    existing,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-hardlink")
    target = root / "artifact.json"
    original = b"original\n"
    if existing:
        target.write_bytes(original)
    root_identity = security_module.private_directory_identity(root)
    alias = tmp_path / "atomic-temp-alias"
    real_replace = security_module.os.replace
    linked = False

    def hardlink_before_replace(source, destination, **kwargs):
        nonlocal linked
        if not linked and str(source).endswith(".tmp"):
            os.link(source, alias, src_dir_fd=kwargs["src_dir_fd"])
            linked = True
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", hardlink_before_replace)

    with pytest.raises(ValueError, match="temp changed"):
        security_module.write_private_bytes_atomic(
            target,
            b"replacement\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )

    assert linked is True
    assert target.read_bytes() == original if existing else not target.exists()
    assert alias.read_bytes() == b""
    assert not list(root.glob(".*.tmp"))
    assert not list(root.glob(".*.bak"))


@pytest.mark.parametrize("existing", (False, True))
def test_atomic_writer_rejects_canonical_root_renamed_after_parent_open(
    tmp_path,
    monkeypatch,
    existing,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-root")
    target = root / "artifact.json"
    original = b"original\n"
    if existing:
        target.write_bytes(original)
    root_identity = security_module.private_directory_identity(root)
    displaced = tmp_path / "atomic-root-displaced"
    real_write_all = security_module._write_all
    swapped = False

    def swap_root_after_parent_open(descriptor, data):
        nonlocal swapped
        real_write_all(descriptor, data)
        if not swapped:
            swapped = True
            root.rename(displaced)
            root.mkdir(mode=0o700)

    monkeypatch.setattr(security_module, "_write_all", swap_root_after_parent_open)

    with pytest.raises(ValueError, match="private root changed"):
        security_module.write_private_bytes_atomic(
            target,
            b"replacement\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )

    assert swapped is True
    assert not target.exists()
    displaced_target = displaced / target.name
    assert (
        displaced_target.read_bytes() == original
        if existing
        else not displaced_target.exists()
    )
    assert not list(displaced.glob(".*.tmp"))
    assert not list(displaced.glob(".*.bak"))


@pytest.mark.parametrize("existing", (False, True))
def test_atomic_writer_rolls_back_if_root_moves_after_replace(
    tmp_path,
    monkeypatch,
    existing,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-post-replace")
    target = root / "artifact.json"
    original = b"original\n"
    if existing:
        target.write_bytes(original)
    root_identity = security_module.private_directory_identity(root)
    displaced = tmp_path / "atomic-post-replace-displaced"
    real_replace = security_module.os.replace
    swapped = False

    def swap_root_after_replace(source, destination, **kwargs):
        nonlocal swapped
        result = real_replace(source, destination, **kwargs)
        if not swapped and str(source).endswith(".tmp"):
            swapped = True
            root.rename(displaced)
            root.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(security_module.os, "replace", swap_root_after_replace)

    with pytest.raises(ValueError, match="private root changed"):
        security_module.write_private_bytes_atomic(
            target,
            b"replacement\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )

    assert swapped is True
    assert not target.exists()
    displaced_target = displaced / target.name
    assert (
        displaced_target.read_bytes() == original
        if existing
        else not displaced_target.exists()
    )
    assert not list(displaced.glob(".*.tmp"))
    assert not list(displaced.glob(".*.bak"))


@pytest.mark.parametrize("existing", (False, True))
def test_atomic_writer_parent_fsync_failure_restores_previous_target(
    tmp_path,
    existing,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-fsync")
    target = root / "artifact.json"
    original = b"original\n"
    if existing:
        target.write_bytes(original)
    root_identity = security_module.private_directory_identity(root)
    calls = 0
    fail_at = 2 if existing else 1

    def fail_commit_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("parent fsync failed")
        os.fsync(descriptor)

    with pytest.raises(OSError, match="parent fsync failed"):
        security_module.write_private_bytes_atomic(
            target,
            b"replacement\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
            fsync_parent=fail_commit_fsync,
        )

    assert target.read_bytes() == original if existing else not target.exists()
    assert not list(root.glob(".*.tmp"))
    assert not list(root.glob(".*.bak"))


@pytest.mark.parametrize("existing", (False, True))
def test_atomic_writer_never_rolls_back_over_unknown_canonical(
    tmp_path,
    monkeypatch,
    existing,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-unknown")
    target = root / "artifact.json"
    original = b"original\n"
    concurrent = b"concurrent\n"
    if existing:
        target.write_bytes(original)
    root_identity = security_module.private_directory_identity(root)
    real_replace = security_module.os.replace

    def fail_before_install(source, destination, **kwargs):
        if str(source).endswith(".tmp"):
            target.unlink(missing_ok=True)
            target.write_bytes(concurrent)
            raise OSError("replace failed before install")
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", fail_before_install)

    with pytest.raises(ValueError, match="private temp changed"):
        security_module.write_private_bytes_atomic(
            target,
            b"writer\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )

    assert target.read_bytes() == concurrent
    backups = list(root.glob(".*.bak"))
    if existing:
        assert len(backups) == 1
        assert backups[0].read_bytes() == original
    else:
        assert backups == []
    assert not list(root.glob(".*.tmp"))


@pytest.mark.parametrize("mutation", ("tamper", "delete"))
def test_atomic_writer_does_not_destroy_new_canonical_when_backup_is_untrusted(
    tmp_path,
    mutation,
):
    root = security_module.ensure_private_dir(tmp_path / f"atomic-backup-{mutation}")
    target = root / "artifact.json"
    target.write_bytes(b"original\n")
    root_identity = security_module.private_directory_identity(root)
    replacement = b"replacement\n"
    calls = 0

    def mutate_backup_then_fail(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            backup = next(root.glob(".*.bak"))
            if mutation == "tamper":
                backup.write_bytes(b"tampered\n")
            else:
                backup.unlink()
            os.fsync(descriptor)
            return
        if calls == 2:
            raise OSError("commit fsync failed")
        os.fsync(descriptor)

    with pytest.raises(ValueError, match="private temp changed"):
        security_module.write_private_bytes_atomic(
            target,
            replacement,
            trusted_root=root,
            trusted_root_identity=root_identity,
            fsync_parent=mutate_backup_then_fail,
        )

    assert target.read_bytes() == replacement
    backups = list(root.glob(".*.bak"))
    if mutation == "tamper":
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"tampered\n"
    else:
        assert backups == []


def test_atomic_writer_rejects_oversized_existing_artifact_before_backup(tmp_path):
    root = security_module.ensure_private_dir(tmp_path / "atomic-bounded")
    target = root / "artifact.json"
    original = b"x" * 9
    target.write_bytes(original)

    with pytest.raises(ValueError, match="private file too large"):
        security_module.write_private_bytes_atomic(
            target,
            b"small\n",
            trusted_root=root,
            trusted_root_identity=security_module.private_directory_identity(root),
            max_existing_bytes=8,
        )

    assert target.read_bytes() == original
    assert not list(root.glob(".*.tmp"))
    assert not list(root.glob(".*.bak"))


def test_atomic_writer_ignores_unlinked_backup_wipe_failure(
    tmp_path,
    monkeypatch,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-cleanup")
    target = root / "artifact.json"
    original = b"old-sensitive-bytes\n"
    replacement = b"new-redacted-bytes\n"
    target.write_bytes(original)

    monkeypatch.setattr(
        security_module.os,
        "ftruncate",
        lambda _descriptor, _length: (_ for _ in ()).throw(
            OSError("backup cleanup failed")
        ),
    )

    assert (
        security_module.write_private_bytes_atomic(
        target,
        replacement,
        trusted_root=root,
        trusted_root_identity=security_module.private_directory_identity(root),
        )
        == target
    )

    assert target.read_bytes() == replacement
    backups = list(root.glob(".*.bak"))
    assert backups == []


def test_atomic_writer_rolls_back_if_committed_backup_unlink_fails(
    tmp_path,
    monkeypatch,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-cleanup-preserved")
    target = root / "artifact.json"
    original = b"old-sensitive-bytes\n"
    replacement = b"new-redacted-bytes\n"
    target.write_bytes(original)
    real_unlink = security_module.os.unlink

    def fail_backup_unlink(name, **kwargs):
        if str(name).endswith(".bak"):
            raise OSError("backup cleanup failed")
        return real_unlink(name, **kwargs)

    monkeypatch.setattr(security_module.os, "unlink", fail_backup_unlink)

    with pytest.raises(OSError, match="backup cleanup failed"):
        security_module.write_private_bytes_atomic(
            target,
            replacement,
            trusted_root=root,
            trusted_root_identity=security_module.private_directory_identity(root),
        )

    assert target.read_bytes() == original
    backups = list(root.glob(".*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_atomic_writer_rebuilds_backup_if_cleanup_parent_fsync_fails(tmp_path):
    root = security_module.ensure_private_dir(tmp_path / "atomic-cleanup-fsync")
    target = root / "artifact.json"
    original = b"original\n"
    target.write_bytes(original)
    calls = 0

    def fail_cleanup_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("cleanup parent fsync failed")
        os.fsync(descriptor)

    with pytest.raises(OSError, match="cleanup parent fsync failed"):
        security_module.write_private_bytes_atomic(
            target,
            b"replacement\n",
            trusted_root=root,
            trusted_root_identity=security_module.private_directory_identity(root),
            fsync_parent=fail_cleanup_fsync,
        )

    assert target.read_bytes() == original
    assert not list(root.glob(".*.bak"))
    assert not list(root.glob(".*.restore"))


def test_atomic_writer_marks_committed_when_cleanup_and_rollback_are_untrusted(
    tmp_path,
    monkeypatch,
):
    root = security_module.ensure_private_dir(tmp_path / "atomic-cleanup-ambiguous")
    target = root / "artifact.json"
    original = b"old-sensitive-bytes\n"
    replacement = b"new-redacted-bytes\n"
    target.write_bytes(original)
    real_unlink = security_module.os.unlink

    def tamper_and_fail_backup_unlink(name, **kwargs):
        if str(name).endswith(".bak"):
            next(root.glob(".*.bak")).write_bytes(b"tampered-old-bytes!\n")
            raise OSError("backup cleanup failed")
        return real_unlink(name, **kwargs)

    monkeypatch.setattr(
        security_module.os,
        "unlink",
        tamper_and_fail_backup_unlink,
    )

    with pytest.raises(security_module.PrivateAtomicWriteError) as raised:
        security_module.write_private_bytes_atomic(
            target,
            replacement,
            trusted_root=root,
            trusted_root_identity=security_module.private_directory_identity(root),
        )

    assert raised.value.committed is True
    assert target.read_bytes() == replacement


def test_block_store_rejects_agent_hardlinks_added_after_construction(tmp_path):
    workspace = tmp_path / "workspace-memory"
    user = tmp_path / "user-memory"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace, user, redaction_env={})
    outside = tmp_path / "outside-memory.md"
    outside.write_text("outside private text\n", encoding="utf-8")
    outside.chmod(0o644)
    agent_notes = workspace / "agent_notes.md"
    os.link(outside, agent_notes)

    assert "workspace/agent_notes.md" not in {entry.path for entry in store.list()}
    with pytest.raises(ValueError, match="link|private"):
        store.read("workspace/agent_notes.md")
    with pytest.raises(ValueError, match="link|private"):
        store.append_agent_note("workspace", "safe note")

    assert outside.read_text(encoding="utf-8") == "outside private text\n"
    _assert_mode(outside, 0o644)
    agent_notes.unlink()
    agent_dir = workspace / "agent"
    agent_dir.mkdir()
    topic = agent_dir / "policy.md"
    os.link(outside, topic)

    assert "workspace/agent/policy.md" not in {entry.path for entry in store.list()}
    with pytest.raises(ValueError, match="invalid memory path"):
        store.read("workspace/agent/policy.md")
    assert not hasattr(store, "write_agent_topic")

    assert outside.read_text(encoding="utf-8") == "outside private text\n"
    _assert_mode(outside, 0o644)


def test_block_store_owned_paths_are_private(tmp_path):
    workspace = tmp_path / "workspace-memory"
    user = tmp_path / "user-memory"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})

    store.append_agent_note("workspace", "safe note")
    store.append_agent_note("user", "safe user note")

    for directory in (workspace, user):
        _assert_mode(directory, 0o700)
    for path in (workspace / "agent_notes.md", user / "agent_notes.md"):
        _assert_mode(path, 0o600)


def test_block_store_ignores_obsolete_agent_files_and_preserves_user_notes(tmp_path):
    workspace = tmp_path / "workspace-memory"
    user = tmp_path / "user-memory"
    agent_dir = workspace / "agent"
    notes_dir = workspace / "notes"
    agent_dir.mkdir(parents=True)
    notes_dir.mkdir()
    nested_user_dir = notes_dir / "agent"
    nested_user_dir.mkdir()
    agent_file = agent_dir / "legacy.md"
    user_note = notes_dir / "source.md"
    nested_user_note = nested_user_dir / "user.md"
    agent_file.write_text("agent\n", encoding="utf-8")
    user_note.write_text("user\n", encoding="utf-8")
    nested_user_note.write_text("nested user\n", encoding="utf-8")
    user.mkdir()
    agent_file.chmod(0o644)
    user_note.chmod(0o644)
    nested_user_note.chmod(0o644)

    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    paths = {entry.path for entry in store.list()}
    assert "workspace/agent/legacy.md" not in paths
    assert "workspace/notes/agent/user.md" in paths

    _assert_mode(workspace, 0o700)
    _assert_mode(user, 0o700)
    _assert_mode(agent_dir, 0o755)
    _assert_mode(agent_file, 0o644)
    _assert_mode(notes_dir, 0o755)
    _assert_mode(user_note, 0o644)
    _assert_mode(nested_user_dir, 0o755)
    _assert_mode(nested_user_note, 0o644)


def test_block_store_temp_swap_preserves_unknown_installed_symlink(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace-memory"
    user = tmp_path / "user-memory"
    workspace.mkdir()
    user.mkdir()
    outside = tmp_path / "outside-memory.md"
    outside.write_text("outside\n", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    original_replace = security_module.os.replace

    def swap_before_replace(source, target, **kwargs):
        if source.endswith(".tmp"):
            parent = kwargs["src_dir_fd"]
            os.unlink(source, dir_fd=parent)
            os.symlink(outside, source, dir_fd=parent)
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", swap_before_replace)

    with pytest.raises(ValueError, match="temp|changed|regular|symlink"):
        store.append_agent_note("workspace", "safe note")

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (workspace / "agent_notes.md").is_symlink()
