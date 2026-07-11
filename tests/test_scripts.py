import subprocess
import sys
from pathlib import Path
import importlib.util

from pico.evaluation import metrics_experiments


def test_ci_tracks_and_uses_frozen_uv_lock():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert Path("uv.lock").is_file()
    assert "uv.lock" not in ignored
    assert 'version: "0.11.26"' in workflow
    assert "      - main" in workflow
    assert "      - memory" in workflow
    assert "run: uv sync --frozen --dev" in workflow


def test_maintenance_scripts_start_and_show_help():
    for script in (
        "scripts/collect_resume_metrics.py",
        "scripts/run_large_scale_experiments.py",
        "scripts/run_provider_experiments.py",
    ):
        result = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_local_check_script_matches_ci_commands():
    script = Path("scripts/check.sh")

    assert script.exists()
    assert script.stat().st_mode & 0o111

    text = script.read_text()
    assert "uv run ruff check ." in text
    assert "uv run pytest -q" in text


def test_provider_experiment_defaults_allow_reasoning_budget():
    spec = importlib.util.spec_from_file_location(
        "run_provider_experiments_script",
        Path("scripts/run_provider_experiments.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_arg_parser().parse_args(["--output-json", "out.json"])

    assert args.max_new_tokens == metrics_experiments.DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS
    assert args.max_new_tokens >= 2048


def test_provider_experiment_parser_accepts_provider_selector():
    spec = importlib.util.spec_from_file_location(
        "run_provider_experiments_script",
        Path("scripts/run_provider_experiments.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    default_args = module.build_arg_parser().parse_args(["--output-json", "out.json"])
    selected_args = module.build_arg_parser().parse_args(
        ["--output-json", "out.json", "--provider", "deepseek"]
    )

    assert default_args.provider == "all"
    assert selected_args.provider == "deepseek"
