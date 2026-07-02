import subprocess
import sys


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
