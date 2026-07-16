import json
import os

from pico.state.checkpoint_store import CheckpointStore
from pico.cli import main
from pico.recovery.manager import RecoveryManager, collect_recovery_review_items
from pico.recovery.models import new_checkpoint_record, new_tool_change_record
from pico.tools.change_recorder import ToolChangeRecorder


def write_restorable_checkpoint(store, tmp_path, checkpoint_id):
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record(checkpoint_id, "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "before_exists": True,
            "before_mode": 0o644,
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "after_exists": True,
            "after_mode": 0o644,
            "expected_current_hash": after["content_hash"],
            "source_tool_change_ids": [],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)
    return record


def test_checkpoints_list_does_not_start_repl(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "checkpoints", "list"])

    assert code == 0
    assert "ckpt_1" in capsys.readouterr().out


def test_checkpoints_list_is_zero_mutation_when_store_is_absent(tmp_path, capsys):
    before = tmp_path.stat()

    assert main(["--cwd", str(tmp_path), "checkpoints", "list"]) == 0

    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".pico").exists()
    assert tmp_path.stat() == before


def test_runs_show_prints_run_artifact(tmp_path, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "task_state.json").write_text('{"run_id": "run_1"}\n', encoding="utf-8")

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1"])

    assert code == 0
    assert "run_1" in capsys.readouterr().out


def test_checkpoints_preview_restore_prints_plan(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    write_restorable_checkpoint(store, tmp_path, "ckpt_1")

    code = main(["--cwd", str(tmp_path), "checkpoints", "preview-restore", "ckpt_1"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Restore plan ckpt_1 (1 entry)" in out
    assert "restore" in out
    assert "note.txt" in out
    assert '"decision"' not in out
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"


def test_checkpoints_preview_restore_json_keeps_success_envelope(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    write_restorable_checkpoint(store, tmp_path, "ckpt_1")

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "preview-restore", "ckpt_1"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["kind"] == "checkpoints_preview_restore"
    assert payload["data"]["checkpoint_id"] == "ckpt_1"
    assert payload["data"]["entries"][0]["decision"] == "restore"


def test_checkpoints_preview_restore_text_explains_ineligible_binary(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    (tmp_path / "image.bin").write_bytes(b"\x00\x01after")
    record = new_checkpoint_record("ckpt_binary", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "image.bin",
            "change_kind": "modified",
            "snapshot_eligible": False,
            "before_blob_ref": "",
            "before_hash": "",
            "before_exists": True,
            "before_mode": 0o644,
            "after_blob_ref": "",
            "after_hash": "",
            "after_exists": True,
            "after_mode": 0o644,
            "expected_current_hash": "",
            "source_tool_change_ids": [],
            "content_kind": "binary",
            "ineligible_reason": "binary_file",
        }
    )
    store.write_checkpoint_record(record)

    code = main(["--cwd", str(tmp_path), "checkpoints", "preview-restore", "ckpt_binary"])

    assert code == 0
    out = capsys.readouterr().out
    assert "review" in out
    assert "image.bin" in out
    assert "no restorable before-state snapshot" in out


def test_checkpoints_restore_without_apply_uses_preview_text(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    write_restorable_checkpoint(store, tmp_path, "ckpt_1")

    code = main(["--cwd", str(tmp_path), "checkpoints", "restore", "ckpt_1"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Restore plan ckpt_1 (1 entry)" in out
    assert "note.txt" in out
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"


def test_checkpoints_restore_apply_changes_disk_state(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    write_restorable_checkpoint(store, tmp_path, "ckpt_1")

    code = main(["--cwd", str(tmp_path), "checkpoints", "restore", "ckpt_1", "--apply"])

    assert code == 0
    assert '"restored_paths": [' in capsys.readouterr().out
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before\n"


def test_checkpoints_prune_accepts_older_than_preview_and_apply(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    old_record = new_checkpoint_record("ckpt_old", "turn", "s", "r", "t", "", str(tmp_path))
    old_record["created_at"] = "2000-01-01T00:00:00+00:00"
    store.write_checkpoint_record(old_record)
    new_record = new_checkpoint_record("ckpt_new", "turn", "s", "r", "t", "", str(tmp_path))
    new_record["created_at"] = "2999-01-01T00:00:00+00:00"
    store.write_checkpoint_record(new_record)

    preview_code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "prune", "--older-than=7d"])
    preview = json.loads(capsys.readouterr().out)

    assert preview_code == 0
    assert preview["kind"] == "checkpoints_prune"
    assert preview["data"]["dry_run"] is True
    assert preview["data"]["prunable_checkpoint_ids"] == ["ckpt_old"]
    assert [item["checkpoint_id"] for item in store.list_checkpoint_records()] == ["ckpt_old", "ckpt_new"]

    apply_code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "prune", "--older-than", "7d", "--apply"])
    applied = json.loads(capsys.readouterr().out)

    assert apply_code == 0
    assert applied["data"]["dry_run"] is False
    assert applied["data"]["removed_checkpoint_ids"] == ["ckpt_old"]
    assert [item["checkpoint_id"] for item in store.list_checkpoint_records()] == ["ckpt_new"]


def test_checkpoints_prune_rejects_invalid_older_than(tmp_path, capsys):
    code = main(["--cwd", str(tmp_path), "checkpoints", "prune", "--older-than=soon"])

    assert code == 2
    assert "older_than must use a duration" in capsys.readouterr().err


def test_checkpoint_commands_accept_unique_id_prefix(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_alpha1234", "turn", "s", "r", "t", "", str(tmp_path)))
    write_restorable_checkpoint(store, tmp_path, "ckpt_restore5678")

    show_code = main(["--cwd", str(tmp_path), "checkpoints", "show", "ckpt_alpha"])
    show_out = capsys.readouterr().out
    preview_code = main(["--cwd", str(tmp_path), "checkpoints", "preview-restore", "ckpt_restore"])
    preview_out = capsys.readouterr().out
    restore_code = main(["--cwd", str(tmp_path), "checkpoints", "restore", "ckpt_restore", "--apply"])
    restore_out = capsys.readouterr().out

    assert show_code == 0
    assert '"checkpoint_id": "ckpt_alpha1234"' in show_out
    assert preview_code == 0
    assert "Restore plan ckpt_restore5678 (1 entry)" in preview_out
    assert restore_code == 0
    assert '"restored_paths": [' in restore_out
    restore_records = [record for record in store.list_checkpoint_records() if record["checkpoint_type"] == "restore"]
    assert restore_records[-1]["parent_checkpoint_id"] == "ckpt_restore5678"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before\n"


def test_checkpoint_prefix_errors_include_candidates(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_abcdef01", "turn", "s", "r", "t", "", str(tmp_path)))
    store.write_checkpoint_record(new_checkpoint_record("ckpt_abcdef99", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "show", "ckpt_abcdef"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "checkpoint_prefix_ambiguous"
    assert payload["error"]["details"]["candidates"] == ["ckpt_abcdef01", "ckpt_abcdef99"]


def test_checkpoints_prune_apply_removes_orphan_blob(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    orphan = store.write_blob(b"orphan", "text")

    code = main(["--cwd", str(tmp_path), "checkpoints", "prune", "--apply"])

    assert code == 0
    assert orphan["blob_ref"] in capsys.readouterr().out
    assert not store.has_blob(orphan["blob_ref"])


def test_checkpoints_restore_rejects_unknown_flag(tmp_path):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "checkpoints", "restore", "ckpt_1", "--aply"])

    assert code == 2


def test_checkpoints_pending_lists_tool_change_and_invalid_record(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {"path": "note.txt"}
    )
    (store.tool_changes_dir / "github_pat_secret_filename.json").write_bytes(
        b"{private-invalid-evidence"
    )
    code = main(
        ["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["kind"] == "checkpoints_pending"
    assert {item["status"] for item in payload["data"]["tool_changes"]} == {
        "pending"
    }
    assert {item["status"] for item in payload["data"]["invalid_records"]} == {
        "invalid_record"
    }
    serialized = json.dumps(payload)
    assert "private-invalid-evidence" not in serialized
    assert "github_pat_secret_filename" not in serialized


def test_collect_recovery_review_items_has_fixed_read_only_shape(tmp_path):
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )
    (store.records_dir / "secret-filename.json").write_bytes(
        b"{invalid-private-bytes"
    )
    before = {
        path: path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    items = collect_recovery_review_items(store, tmp_path)
    after = {
        path: path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert set(items) == {
        "tool_changes",
        "restore_journals",
        "invalid_records",
        "quarantined_records",
    }
    assert items["tool_changes"][0]["status"] == "pending"
    assert items["restore_journals"] == []
    assert items["invalid_records"][0]["opaque_id"].startswith("invalid_")
    assert items["quarantined_records"] == []
    assert "secret-filename" not in json.dumps(items)
    assert "invalid-private-bytes" not in json.dumps(items)
    assert before == after


def test_resolve_pending_defaults_to_read_only_preview(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )
    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "checkpoints",
            "resolve-pending",
            pending["tool_change_id"],
        ]
    )
    assert code == 0
    assert store.load_tool_change_record(pending["tool_change_id"])["status"] == "pending"
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "pending"


def test_resolve_pending_apply_interrupts_with_review_metadata(tmp_path):
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )
    code = main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            pending["tool_change_id"],
            "--apply",
        ]
    )
    record = store.load_tool_change_record(pending["tool_change_id"])
    assert code == 0
    assert record["status"] == "interrupted"
    assert record["reviewed_by"] == "cli"


def test_resolve_terminal_interrupted_marks_existing_review_complete(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    pending = recorder.start(
        "", "turn-1", "write_file", "workspace_write", {}
    )
    recorder.finalize(
        pending["tool_change_id"],
        "interrupted",
        affected_paths=["x.txt"],
    )

    reviews = collect_recovery_review_items(store, tmp_path)
    assert reviews["tool_changes"] == [
        {
            "tool_change_id": pending["tool_change_id"],
            "status": "interrupted",
            "owner_id": "owner-a",
            "tool_name": "write_file",
            "effect_class": "workspace_write",
            "started_at": pending["started_at"],
        }
    ]

    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            pending["tool_change_id"],
            "--apply",
        ]
    ) == 0
    record = store.load_tool_change_record(pending["tool_change_id"])
    assert record["status"] == "interrupted"
    assert record["reviewed_by"] == "cli"
    assert record["reviewed_at"]
    assert collect_recovery_review_items(store, tmp_path)["tool_changes"] == []


def test_resolve_pending_rejects_cross_kind_ambiguous_id(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    shared_id = "shared_review_id"
    tool = new_tool_change_record(
        shared_id, "", "turn", "write_file", "workspace_write", "owner"
    )
    store.write_tool_change_record(tool)
    restore = new_checkpoint_record(
        shared_id,
        "restore",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    restore["status"] = "applying"
    restore["restore_provenance"] = {"entries": []}
    store.write_checkpoint_record(restore)
    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "checkpoints",
            "resolve-pending",
            shared_id,
            "--apply",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error"]["code"] == "recovery_review_ambiguous"
    assert store.load_tool_change_record(shared_id)["status"] == "pending"
    assert store.load_checkpoint_record(shared_id)["status"] == "applying"


def test_resolve_invalid_apply_quarantines_without_deleting_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{private-invalid-evidence"
    source = store.records_dir / "secret-token-filename.json"
    source.write_bytes(raw)
    [invalid] = store.list_checkpoint_records(strict=False)
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            invalid["opaque_id"],
        ]
    ) == 0
    assert source.read_bytes() == raw
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            invalid["opaque_id"],
            "--apply",
        ]
    ) == 0
    inspected = store.list_quarantined_records()[0]
    assert inspected["opaque_id"] == invalid["opaque_id"]
    assert (store.root / inspected["quarantine_raw_path"]).read_bytes() == raw


def test_resolve_non_regular_invalid_apply_moves_inode_without_following(tmp_path):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-private"
    outside.write_bytes(b"must-not-be-read-or-moved")
    source = store.records_dir / "linked.json"
    source.symlink_to(outside)
    [invalid] = store.list_checkpoint_records(strict=False)
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            invalid["opaque_id"],
        ]
    ) == 0
    assert os.path.lexists(source)
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            invalid["opaque_id"],
            "--apply",
        ]
    ) == 0
    assert not os.path.lexists(source)
    assert outside.read_bytes() == b"must-not-be-read-or-moved"


def test_quarantined_record_remains_visible_as_inactive_inspection(
    tmp_path, capsys
):
    store = CheckpointStore(tmp_path)
    (store.records_dir / "secret-filename.json").write_bytes(b"{invalid")
    [invalid] = store.list_checkpoint_records(strict=False)
    store.quarantine_invalid_record(
        invalid["opaque_id"], expected_raw_hash=invalid["raw_hash"]
    )
    code = main(
        ["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert any(
        item.get("opaque_id") == invalid["opaque_id"]
        and item.get("status") == "quarantined"
        for item in payload["data"]["quarantined_records"]
    )
    assert all(
        item.get("opaque_id") != invalid["opaque_id"]
        for item in payload["data"]["invalid_records"]
    )


def test_partial_review_requires_preview_then_explicit_apply_acceptance(
    tmp_path, capsys
):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_partial_review",
        "restore",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    record["status"] = "partial"
    record["restore_provenance"] = {
        "entries": [
            {
                "path": "note.txt",
                "pre_state": {
                    "exists": False,
                    "hash": "",
                    "blob_ref": "",
                    "mode": None,
                },
                "planned_post_state": {
                    "exists": False,
                    "hash": "",
                    "blob_ref": "",
                    "mode": None,
                },
                "outcome": "uncertain",
                "reason": "manual_recovery_required",
                "target_modified": True,
                "actual_post_state": {},
            }
        ]
    }
    store.write_checkpoint_record(record)
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "checkpoints",
            "resolve-pending",
            record["checkpoint_id"],
        ]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["data"]["status"] == "partial_review_required"
    assert store.load_checkpoint_record(record["checkpoint_id"])["reviewed_at"] == ""
    assert main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "resolve-pending",
            record["checkpoint_id"],
            "--apply",
        ]
    ) == 0
    accepted = store.load_checkpoint_record(record["checkpoint_id"])
    assert accepted["status"] == "partial"
    assert accepted["reviewed_at"]
    assert accepted["restore_provenance"]["entries"][0]["outcome"] == "uncertain"


def test_blocked_and_partial_restore_apply_return_runtime_exit(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    checkpoint = write_restorable_checkpoint(store, tmp_path, "ckpt_exit")
    (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")
    blocked = main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "restore",
            checkpoint["checkpoint_id"],
            "--apply",
        ]
    )
    assert blocked == 1
    monkeypatch.setattr(
        RecoveryManager,
        "apply_restore",
        lambda self, checkpoint_id: {
            "status": "partial",
            "restore_checkpoint_id": "ckpt_partial_result",
        },
    )
    partial = main(
        [
            "--cwd",
            str(tmp_path),
            "checkpoints",
            "restore",
            checkpoint["checkpoint_id"],
            "--apply",
        ]
    )
    assert partial == 1


def test_checkpoints_prune_rejects_unknown_flag(tmp_path):
    code = main(["--cwd", str(tmp_path), "checkpoints", "prune", "--bogus"])

    assert code == 2


def test_runs_show_rejects_extra_args(tmp_path):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1", "extra"])

    assert code == 2


def test_invalid_checkpoints_subcommand_is_usage_error_without_agent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pico.cli.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), "checkpoints", "look", "good"])

    assert code == 2


def test_run_accepts_prompt_starting_with_namespace(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["cwd"] = args.cwd
        called["prompt"] = list(args.prompt)

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")

    code = main(["--cwd", str(tmp_path), "run", "checkpoints", "look", "good"])

    assert code == 0
    assert called["asked"] == "checkpoints look good"
    assert "answer" in capsys.readouterr().out


def test_no_argument_cli_shows_root_help_without_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pico.cli.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path)])

    assert code == 0
    assert capsys.readouterr().out.startswith("pico — Local coding agent")


def test_no_input_blocks_repl_before_input(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("input called")))

    code = main(["--cwd", str(tmp_path), "--no-input", "repl"])

    assert code == 2
    assert "--no-input" in capsys.readouterr().err


def test_quiet_suppresses_welcome_for_run(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "WELCOME")

    code = main(["--cwd", str(tmp_path), "--quiet", "run", "fix"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "WELCOME" not in out


def test_checkpoints_list_json_uses_success_envelope(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "list"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["kind"] == "checkpoints_list"
    assert payload["data"][0]["checkpoint_id"] == "ckpt_1"


def test_runs_list_json_uses_success_envelope(tmp_path, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "--format", "json", "runs", "list"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "kind": "runs_list", "data": [{"run_id": "run_1"}]}


def test_json_output_contains_no_human_tip_text(tmp_path, capsys):
    code = main(["--cwd", str(tmp_path), "--format", "json", "runs", "list"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.strip().startswith("{")
    assert "Tip:" not in out
    json.loads(out)


def test_quiet_suppresses_text_inspection_output(tmp_path, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "--quiet", "runs", "list"])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_collect_recovery_review_items_has_stable_shape(tmp_path):
    from pico.state.checkpoint_store import CheckpointStore
    from pico.cli.recovery import collect_recovery_review_items

    payload = collect_recovery_review_items(CheckpointStore(tmp_path), tmp_path)

    assert payload == {
        "tool_changes": [],
        "restore_journals": [],
        "invalid_records": [],
        "quarantined_records": [],
    }
