import json

import pytest

from pony.state.checkpoint_store import CheckpointStore
from pony.cli.app import main
from pony.recovery.manager import collect_recovery_review_items
from pony.recovery.models import new_checkpoint_record
from pony.tools.change_recorder import ToolChangeRecorder


def write_restorable_checkpoint(store, tmp_path, checkpoint_id):
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record(
        checkpoint_id, "turn", "s", "r", "t", "", str(tmp_path)
    )
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
    store.write_checkpoint_record(
        new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    )

    code = main(["--cwd", str(tmp_path), "checkpoints", "list"])

    assert code == 0
    assert "ckpt_1" in capsys.readouterr().out


def test_checkpoints_list_is_zero_mutation_when_store_is_absent(tmp_path, capsys):
    before = tmp_path.stat()

    assert main(["--cwd", str(tmp_path), "checkpoints", "list"]) == 0

    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".pony").exists()
    assert tmp_path.stat() == before


@pytest.mark.parametrize(
    "tokens",
    (
        ["preview-restore", "ckpt_1"],
        ["restore", "ckpt_1", "--apply"],
        ["resolve-pending", "change_1", "--apply"],
        ["prune", "--apply"],
    ),
)
def test_removed_checkpoint_mutations_are_rejected_without_state_write(
    tmp_path, capsys, tokens
):
    code = main(["--cwd", str(tmp_path), "checkpoints", *tokens])

    assert code == 2
    assert "usage: pony checkpoints {list | show <id> | pending}" in capsys.readouterr().err
    assert not (tmp_path / ".pony").exists()


def test_runs_show_prints_run_artifact(tmp_path, capsys):
    run_dir = tmp_path / ".pony" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "task_state.json").write_text('{"run_id": "run_1"}\n', encoding="utf-8")

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1"])

    assert code == 0
    assert "run_1" in capsys.readouterr().out


def test_checkpoint_show_accepts_unique_id_prefix(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(
        new_checkpoint_record(
            "ckpt_alpha1234", "turn", "s", "r", "t", "", str(tmp_path)
        )
    )
    show_code = main(["--cwd", str(tmp_path), "checkpoints", "show", "ckpt_alpha"])
    show_out = capsys.readouterr().out

    assert show_code == 0
    assert '"checkpoint_id": "ckpt_alpha1234"' in show_out


def test_checkpoint_prefix_errors_include_candidates(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(
        new_checkpoint_record("ckpt_abcdef01", "turn", "s", "r", "t", "", str(tmp_path))
    )
    store.write_checkpoint_record(
        new_checkpoint_record("ckpt_abcdef99", "turn", "s", "r", "t", "", str(tmp_path))
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "checkpoints",
            "show",
            "ckpt_abcdef",
        ]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "checkpoint_prefix_ambiguous"
    assert payload["error"]["details"]["candidates"] == [
        "ckpt_abcdef01",
        "ckpt_abcdef99",
    ]


def test_checkpoints_pending_lists_tool_change_and_invalid_record(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {"path": "note.txt"}
    )
    (store.tool_changes_dir / "github_pat_secret_filename.json").write_bytes(
        b"{private-invalid-evidence"
    )
    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["kind"] == "checkpoints_pending"
    assert {item["status"] for item in payload["data"]["tool_changes"]} == {"pending"}
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
    (store.records_dir / "secret-filename.json").write_bytes(b"{invalid-private-bytes")
    before = {
        path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()
    }
    items = collect_recovery_review_items(store, tmp_path)
    after = {
        path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()
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


def test_quarantined_record_remains_visible_as_inactive_inspection(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    (store.records_dir / "secret-filename.json").write_bytes(b"{invalid")
    [invalid] = store.list_checkpoint_records(strict=False)
    store.quarantine_invalid_record(
        invalid["opaque_id"], expected_raw_hash=invalid["raw_hash"]
    )
    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"])
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


def test_runs_show_rejects_extra_args(tmp_path):
    run_dir = tmp_path / ".pony" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "runs", "show", "run_1", "extra"])

    assert code == 2


def test_invalid_checkpoints_subcommand_is_usage_error_without_agent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
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
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pony.cli.app.build_agent", fake_build_agent)

    code = main(["--cwd", str(tmp_path), "run", "checkpoints", "look", "good"])

    assert code == 0
    assert called["asked"] == "checkpoints look good"
    assert "answer" in capsys.readouterr().out


def test_explicit_help_shows_root_help_without_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), "--help"])

    assert code == 0
    assert capsys.readouterr().out.startswith("pony — Local coding agent")


def test_no_input_blocks_repl_before_input(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            session = {"id": "s"}

        return FakeAgent()

    monkeypatch.setattr("pony.cli.app.build_agent", fake_build_agent)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: (_ for _ in ()).throw(AssertionError("input called")),
    )

    code = main(["--cwd", str(tmp_path), "--no-input", "repl"])

    assert code == 2
    assert "--no-input" in capsys.readouterr().err


def test_run_output_has_no_decorative_banner(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pony.cli.app.build_agent", fake_build_agent)

    code = main(["--cwd", str(tmp_path), "run", "fix"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "answer"


def test_checkpoints_list_json_uses_success_envelope(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(
        new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    )

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "list"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["kind"] == "checkpoints_list"
    assert payload["data"][0]["checkpoint_id"] == "ckpt_1"


def test_runs_list_json_uses_success_envelope(tmp_path, capsys):
    run_dir = tmp_path / ".pony" / "runs" / "run_1"
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
    run_dir = tmp_path / ".pony" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "--quiet", "runs", "list"])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_collect_recovery_review_items_has_stable_shape(tmp_path):
    from pony.state.checkpoint_store import CheckpointStore
    from pony.cli.recovery import collect_recovery_review_items

    payload = collect_recovery_review_items(CheckpointStore(tmp_path), tmp_path)

    assert payload == {
        "tool_changes": [],
        "restore_journals": [],
        "invalid_records": [],
        "quarantined_records": [],
    }
