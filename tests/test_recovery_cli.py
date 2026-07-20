import json
import os
import stat

import pytest

from pony.cli.app import main


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _legacy_checkpoint_root(root):
    _private_directory(root / ".pony")
    return _private_directory(root / ".pony" / "checkpoints")


def _write_legacy_checkpoint(root, checkpoint_id):
    records = _private_directory(_legacy_checkpoint_root(root) / "records")
    path = records / f"{checkpoint_id}.json"
    path.write_text(
        json.dumps(
            {
                "record_type": "checkpoint",
                "format_version": 1,
                "checkpoint_id": checkpoint_id,
                "checkpoint_type": "turn",
                "created_at": "now",
                "status": "",
                "owner_id": "",
                "reviewed_at": "",
                "private_payload": "not exposed",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _write_legacy_tool_change(root, tool_change_id, *, status="pending"):
    changes = _private_directory(_legacy_checkpoint_root(root) / "tool_changes")
    path = changes / f"{tool_change_id}.json"
    path.write_text(
        json.dumps(
            {
                "record_type": "tool_change",
                "format_version": 2,
                "tool_change_id": tool_change_id,
                "status": status,
                "owner_id": "owner-a",
                "tool_name": "write_file",
                "effect_class": "workspace_write",
                "started_at": "now",
                "reviewed_at": "",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_checkpoints_list_does_not_start_repl(tmp_path, capsys):
    _write_legacy_checkpoint(tmp_path, "ckpt_1")

    code = main(["--cwd", str(tmp_path), "checkpoints", "list"])

    assert code == 0
    assert "ckpt_1" in capsys.readouterr().out


def test_checkpoints_list_is_zero_mutation_when_store_is_absent(tmp_path, capsys):
    before = tmp_path.stat()

    assert main(["--cwd", str(tmp_path), "checkpoints", "list"]) == 0

    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".pony").exists()
    assert tmp_path.stat() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
@pytest.mark.parametrize("subcommand", (("list",), ("pending",), ("show", "ckpt_1")))
def test_checkpoint_inspection_does_not_harden_project_env(tmp_path, capsys, subcommand):
    _write_legacy_checkpoint(tmp_path, "ckpt_1")
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_API_KEY=inspection-secret\n", encoding="utf-8")
    env_path.chmod(0o644)
    before = env_path.read_bytes()

    assert main(["--cwd", str(tmp_path), "checkpoints", *subcommand]) == 0

    capsys.readouterr()
    assert env_path.read_bytes() == before
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o644


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
    _write_legacy_checkpoint(tmp_path, "ckpt_alpha1234")
    show_code = main(["--cwd", str(tmp_path), "checkpoints", "show", "ckpt_alpha"])
    show_out = capsys.readouterr().out

    assert show_code == 0
    assert '"checkpoint_id": "ckpt_alpha1234"' in show_out


def test_checkpoint_prefix_errors_include_candidates(tmp_path, capsys):
    _write_legacy_checkpoint(tmp_path, "ckpt_abcdef01")
    _write_legacy_checkpoint(tmp_path, "ckpt_abcdef99")

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
    changes = _private_directory(_legacy_checkpoint_root(tmp_path) / "tool_changes")
    _write_legacy_tool_change(tmp_path, "change_1")
    invalid = changes / "github_pat_secret_filename.json"
    invalid.write_bytes(
        b"{private-invalid-evidence"
    )
    invalid.chmod(0o600)
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
    _write_legacy_checkpoint(tmp_path, "ckpt_1")

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
