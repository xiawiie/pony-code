import subprocess
import sys
from pathlib import Path


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
