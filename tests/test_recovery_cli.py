import json

from pico.checkpoint_store import CheckpointStore
from pico.cli import main
from pico.recovery_models import new_checkpoint_record


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
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
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


def test_checkpoints_prune_rejects_unknown_flag(tmp_path):
    code = main(["--cwd", str(tmp_path), "checkpoints", "prune", "--bogus"])

    assert code == 2


def test_runs_show_rejects_extra_args(tmp_path):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1", "extra"])

    assert code == 2


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


def test_legacy_prompt_still_runs_one_shot(tmp_path, monkeypatch, capsys):
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

    code = main(["--cwd", str(tmp_path), "inspect", "tests"])

    assert code == 0
    assert called["asked"] == "inspect tests"
    assert "answer" in capsys.readouterr().out


def test_no_argument_cli_enters_repl_and_exits_on_eof(tmp_path, monkeypatch):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def memory_text(self):
                return ""

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    def fake_input(prompt):
        called["input"] = True
        raise EOFError

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")
    monkeypatch.setattr("builtins.input", fake_input)

    code = main(["--cwd", str(tmp_path)])

    assert code == 0
    assert called["built"] is True
    assert called["input"] is True


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
