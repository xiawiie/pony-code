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
