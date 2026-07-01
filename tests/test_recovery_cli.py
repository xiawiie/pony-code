from pico.checkpoint_store import CheckpointStore
from pico.cli import main
from pico.recovery_models import new_checkpoint_record


def test_checkpoints_list_does_not_start_repl(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "checkpoints", "list"])

    assert code == 0
    assert "ckpt_1" in capsys.readouterr().out


def test_runs_show_prints_run_artifact(tmp_path, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "task_state.json").write_text('{"run_id": "run_1"}\n', encoding="utf-8")

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1"])

    assert code == 0
    assert "run_1" in capsys.readouterr().out


def test_checkpoints_preview_restore_prints_plan(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)

    code = main(["--cwd", str(tmp_path), "checkpoints", "preview-restore", "ckpt_1"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"checkpoint_id": "ckpt_1"' in out
    assert '"decision": "restore"' in out


def test_checkpoints_restore_apply_changes_disk_state(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)

    code = main(["--cwd", str(tmp_path), "checkpoints", "restore", "ckpt_1", "--apply"])

    assert code == 0
    assert '"restored_paths": [' in capsys.readouterr().out
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before\n"


def test_checkpoints_prune_apply_removes_orphan_blob(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    orphan = store.write_blob(b"orphan", "text")

    code = main(["--cwd", str(tmp_path), "checkpoints", "prune", "--apply"])

    assert code == 0
    assert orphan["blob_ref"] in capsys.readouterr().out
    assert not store.has_blob(orphan["blob_ref"])


def test_prompt_starting_with_checkpoints_word_is_not_hijacked(tmp_path, monkeypatch):
    # 保护性回归：`pico "checkpoints look good"` 应该走模型，不被当成子命令。
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
                return "ok"

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")

    code = main(["--cwd", str(tmp_path), "checkpoints", "look", "good"])

    assert code == 0
    assert called["asked"] == "checkpoints look good"
