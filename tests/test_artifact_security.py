import json
import os
from pathlib import Path
import stat

import pytest

from pico import security as security_module
from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.checkpoint_store import CheckpointStore
from pico.cli import main
from pico.cli_session import inspect_session
from pico.memory.block_store import BlockStore
from pico.recovery_models import new_checkpoint_record, new_tool_change_record
from pico.run_store import RunStore
from pico.task_state import TaskState


def _build_agent(root, *, secret_env_names=()):
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy="auto",
        secret_env_names=secret_env_names,
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


def _session(session_id):
    return {
        "record_type": "session",
        "format_version": 1,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": "/repo",
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
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    agent = _build_agent(tmp_path, secret_env_names=("PICO_TEST_TOKEN",))
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

    for path in (tmp_path / ".pico").rglob("*"):
        if (
            path.is_file()
            and "/sessions/backup/" not in path.as_posix()
            and "/blobs/" not in path.as_posix()
        ):
            assert secret.encode() not in path.read_bytes(), path

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "show",
        "ckpt_canary",
    ]) == 0
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
            new_tool_change_record(
                "tc_fifo", "", "t", "write_file", "workspace_write"
            )
        )

    data = b"exact blob bytes"
    from pico.recovery_paths import hash_bytes

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


def test_checkpoint_temp_swap_is_detected_without_installing_symlink(
    tmp_path,
    monkeypatch,
):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-temp.json"
    outside.write_text("outside\n", encoding="utf-8")
    from pico import security as security_module

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
    assert not store._record_path("ckpt_swap").exists()


def test_legacy_inspection_redacts_process_and_project_collision(
    tmp_path,
    monkeypatch,
    capsys,
):
    process_secret = "opaque-process-secret-123456789"
    project_secret = "opaque-project-secret-987654321"
    collision_secret = "opaque-existing-collision-246813579"
    monkeypatch.setenv("PICO_TEST_TOKEN", process_secret)
    monkeypatch.setenv("PICO_REDACTION_COLLISION_0_SECRET", collision_secret)
    (tmp_path / ".env").write_text(
        f"PICO_TEST_TOKEN={project_secret}\n",
        encoding="utf-8",
    )
    legacy_text = f"{process_secret} {project_secret} {collision_secret}"

    sessions = tmp_path / ".pico" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "legacy.json").write_text(
        json.dumps({"id": "legacy", "message": legacy_text}),
        encoding="utf-8",
    )
    runs = tmp_path / ".pico" / "runs" / "legacy"
    runs.mkdir(parents=True)
    (runs / "report.json").write_text(
        json.dumps({"message": legacy_text}),
        encoding="utf-8",
    )
    checkpoints = tmp_path / ".pico" / "checkpoints" / "records"
    checkpoints.mkdir(parents=True)
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
    (checkpoints / "ckpt_legacy.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )

    commands = (
        ["sessions", "show", "legacy"],
        ["runs", "show", "legacy"],
        ["checkpoints", "show", "ckpt_legacy"],
    )
    for command in commands:
        for output_format in ("text", "json"):
            assert main([
                "--cwd",
                str(tmp_path),
                "--format",
                output_format,
                *command,
            ]) == 0
            output = capsys.readouterr().out
            assert process_secret not in output
            assert project_secret not in output
            assert collision_secret not in output
            assert "<redacted>" in output


def test_runs_show_redacts_structured_json_before_rendering(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = 'opaque"quote\\value-123456789'
    monkeypatch.setenv("CUSTOM_OPAQUE", secret)
    run_dir = tmp_path / ".pico" / "runs" / "escaped"
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
        assert main([
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_OPAQUE",
            "--format",
            output_format,
            "runs",
            "show",
            "escaped",
        ]) == 0
        output = capsys.readouterr().out
        assert secret not in output
        assert escaped not in output
        assert "<redacted>" in output


def test_runs_show_rejects_path_escape_and_symlink(tmp_path, capsys):
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir()
    secret = "outside-run-secret"
    (outside_run / "report.json").write_text(secret, encoding="utf-8")
    runs_root = tmp_path / ".pico" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "linked").symlink_to(outside_run, target_is_directory=True)

    for run_id in ("../../outside-run", "linked"):
        code = main([
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "runs",
            "show",
            run_id,
        ])
        captured = capsys.readouterr()
        assert code == 2
        assert secret not in captured.out + captured.err


def test_checkpoint_prefix_never_resolves_traversing_record_id(tmp_path, capsys):
    records = tmp_path / ".pico" / "checkpoints" / "records"
    records.mkdir(parents=True)
    malicious_id = "abcdef/../../../outside"
    (records / "entry.json").write_text(
        json.dumps({
            "checkpoint_id": malicious_id,
            "checkpoint_type": "turn",
            "created_at": "2026-07-11T00:00:00Z",
        }),
        encoding="utf-8",
    )
    outside = tmp_path / ".pico" / "outside.json"
    secret = "outside-checkpoint-secret"
    outside.write_text(json.dumps({"message": secret}), encoding="utf-8")

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "show",
        "abcdef",
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.out + captured.err


def test_checkpoint_cli_rejects_symlink_record_as_stable_error(tmp_path, capsys):
    records = tmp_path / ".pico" / "checkpoints" / "records"
    records.mkdir(parents=True)
    outside = tmp_path / "outside-checkpoint.json"
    secret = "outside-symlink-checkpoint-secret"
    outside.write_text(json.dumps({"message": secret}), encoding="utf-8")
    (records / "linked.json").symlink_to(outside)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "checkpoints",
        "list",
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "unsafe_artifact" in captured.out
    assert secret not in captured.out + captured.err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_runs_show_rejects_fifo_without_opening_it(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "fifo"
    run_dir.mkdir(parents=True)
    fifo = run_dir / "report.json"
    os.mkfifo(fifo)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == fifo:
            raise AssertionError("unsafe FIFO read attempted")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "runs",
        "show",
        "fifo",
    ])

    assert code == 2
    assert "unsafe FIFO read attempted" not in capsys.readouterr().out


def test_run_store_rejects_ids_that_escape_its_root(tmp_path):
    root = tmp_path / ".pico" / "runs"
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
    run_store = RunStore(tmp_path / ".pico" / "runs")
    session_store = SessionStore(tmp_path / ".pico" / "sessions")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=session_store,
        run_store=run_store,
        approval_policy="never",
        depth=1,
        redaction_env={"CUSTOM_OPAQUE": secret},
        secret_env_names=("CUSTOM_OPAQUE",),
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
    runs_root = tmp_path / ".pico" / "runs"
    run_dir = runs_root / "legacy"
    run_dir.mkdir(parents=True)
    run_file = run_dir / "report.json"
    run_file.write_text("{}\n", encoding="utf-8")
    sessions_root = tmp_path / ".pico" / "sessions"
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
    sessions_root = tmp_path / ".pico" / "sessions"
    backup = sessions_root / "backup"
    backup.mkdir(parents=True)
    outside_base = tmp_path / "absolute-session"
    outside = outside_base.with_suffix(".json")
    outside.write_text('{"schema_version": 3, "messages": []}\n', encoding="utf-8")
    parent_outside = sessions_root.parent / "parent.json"
    parent_outside.write_text('{}\n', encoding="utf-8")
    backup_file = backup / "raw.json"
    backup_file.write_text('{}\n', encoding="utf-8")
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
        json.dumps({
            "record_type": "session",
            "format_version": 1,
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
        }) + "\n",
        encoding="utf-8",
    )
    assert inspect_session("safe", sessions_root)[0] is True


def test_singular_session_inspection_does_not_echo_invalid_role(tmp_path):
    sessions_root = tmp_path / ".pico" / "sessions"
    sessions_root.mkdir(parents=True)
    secret = "opaque-invalid-role-secret-123456789"
    (sessions_root / "legacy.json").write_text(
        json.dumps({
            "schema_version": 3,
            "messages": [{"role": secret, "content": "safe"}],
        }),
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
    sessions_root = tmp_path / ".pico" / "sessions"
    sessions_root.mkdir(parents=True)
    secret = "opaque-session-summary-secret-123456789"
    monkeypatch.setenv("CUSTOM_SESSION_TOKEN", secret)
    (sessions_root / "legacy.json").write_text(
        json.dumps({"schema_version": secret, "messages": []}),
        encoding="utf-8",
    )

    code = main([
        "--cwd",
        str(tmp_path),
        "--secret-env-name",
        "CUSTOM_SESSION_TOKEN",
        "session",
        "inspect",
        "legacy",
    ])

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
        store.write_checkpoint_record(new_checkpoint_record(
            f"{secret}{suffix}",
            "turn",
            "s",
            "r",
            "t",
            "",
            str(tmp_path),
        ))

    for output_format in ("text", "json"):
        code = main([
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_CHECKPOINT_TOKEN",
            "--format",
            output_format,
            "checkpoints",
            "show",
            secret,
        ])
        captured = capsys.readouterr()
        assert code == 2
        assert secret not in captured.out + captured.err


def test_status_redacts_latest_artifact_ids(tmp_path, monkeypatch, capsys):
    secret = "opaque-status-secret-123456789"
    monkeypatch.setenv("CUSTOM_STATUS_TOKEN", secret)
    (tmp_path / ".env").write_text(
        f"PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_MODEL={secret}\n",
        encoding="utf-8",
    )
    (tmp_path / ".pico" / "runs" / secret).mkdir(parents=True)

    for output_format in ("text", "json"):
        code = main([
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "CUSTOM_STATUS_TOKEN",
            "--format",
            output_format,
            "status",
        ])
        output = capsys.readouterr().out
        assert code == 0
        assert secret not in output
        assert "<redacted>" in output


def test_status_does_not_follow_symlinked_pico_ancestor(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    (outside / "runs" / "external-run").mkdir(parents=True)
    (workspace / ".pico").symlink_to(outside, target_is_directory=True)

    code = main([
        "--cwd",
        str(workspace),
        "--format",
        "json",
        "status",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["storage"]["runs"] is False
    assert payload["data"]["latest"]["run_id"] is None


def test_config_and_doctor_redact_configured_model_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "opaque-diagnostics-model-secret-123456789"
    monkeypatch.setenv("CUSTOM_DIAGNOSTIC_TOKEN", secret)
    (tmp_path / ".env").write_text(
        f"PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_MODEL={secret}\n",
        encoding="utf-8",
    )

    for command in (("config", "show"), ("doctor", "--offline")):
        for output_format in ("text", "json"):
            code = main([
                "--cwd",
                str(tmp_path),
                "--secret-env-name",
                "CUSTOM_DIAGNOSTIC_TOKEN",
                "--format",
                output_format,
                *command,
            ])
            output = capsys.readouterr().out
            assert code == 0
            assert secret not in output


def test_owned_store_constructors_reject_hardlinks_without_chmod(tmp_path):
    cases = []

    run_root = tmp_path / "run-case" / ".pico" / "runs"
    run_file = run_root / "legacy" / "report.json"
    cases.append((run_file, lambda: RunStore(run_root)))

    session_root = tmp_path / "session-case" / ".pico" / "sessions"
    session_file = session_root / "legacy.json"
    cases.append((session_file, lambda: SessionStore(session_root)))

    checkpoint_root = tmp_path / "checkpoint-case"
    checkpoint_file = checkpoint_root / ".pico" / "checkpoints" / "records" / "legacy.json"
    cases.append((checkpoint_file, lambda: CheckpointStore(checkpoint_root)))

    memory_workspace = tmp_path / "memory-case" / "workspace"
    memory_user = tmp_path / "memory-case" / "user"
    memory_file = memory_workspace / "agent_notes.md"
    cases.append((
        memory_file,
        lambda: BlockStore(memory_workspace, memory_user, redaction_env={}),
    ))

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
    store = RunStore(tmp_path / ".pico" / "runs")
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


def test_session_temp_swap_is_removed_without_touching_external_target(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pico" / "sessions")
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
        store.save(_session("swapped"))

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not store.path("swapped").exists()
    assert not store.path("swapped").is_symlink()


def test_store_redactors_cannot_mutate_callers_or_leave_failed_trace(tmp_path):
    def mutating_redactor(value):
        value["mutated"] = True
        return value

    run_store = RunStore(tmp_path / ".pico" / "runs", redactor=mutating_redactor)
    state = TaskState.create(run_id="isolated", task_id="task", user_request="safe")
    run_store.start_run(state)
    event = {"event": "safe"}
    report = {"status": "safe"}
    run_store.append_trace(state, event)
    run_store.write_report(state, report)
    assert event == {"event": "safe"}
    assert report == {"status": "safe"}

    session_store = SessionStore(
        tmp_path / ".pico" / "sessions",
        redactor=mutating_redactor,
    )
    session = _session("isolated")
    session_store.save(session)
    assert session == _session("isolated")

    def failing_redactor(_value):
        raise RuntimeError("redactor failed")

    failing_store = RunStore(
        tmp_path / ".pico" / "failed-runs",
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
    session_store = SessionStore(tmp_path / "session" / ".pico" / "sessions")
    run_store = RunStore(tmp_path / "run" / ".pico" / "runs")
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
            lambda: session_store.save(_session("hardlinked_temp")),
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
        with pytest.raises(ValueError, match="link|private|temp"):
            write()
        assert not canonical.exists()


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


def test_block_store_temp_swap_is_detected_without_installing_symlink(
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
    original_replace = Path.replace

    def swap_before_replace(path, target):
        if path.name.endswith(".tmp"):
            path.unlink()
            path.symlink_to(outside)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", swap_before_replace)

    with pytest.raises(ValueError, match="temp|changed|regular|symlink"):
        store.append_agent_note("workspace", "safe note")

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not (workspace / "agent_notes.md").exists()
