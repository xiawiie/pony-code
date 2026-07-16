import subprocess
import sys
from pathlib import Path
import importlib.util

import pico.evaluation.provider_benchmark as provider_benchmark
import pytest


def test_ci_tracks_and_uses_frozen_uv_lock():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert Path("uv.lock").is_file()
    assert "uv.lock" not in ignored
    assert 'version: "0.11.26"' in workflow
    assert "      - main" in workflow
    assert "      - memory" in workflow
    assert "run: uv sync --frozen --dev" in workflow


def test_linux_ci_does_not_claim_the_darwin_performance_baseline():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "--suite core-functional" in workflow
    assert "--suite core-full" not in workflow


def test_ci_has_macos_security_and_durability_gate():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "runs-on: macos-latest" in workflow
    assert 'python-version: "3.12"' in workflow
    assert workflow.count("run: uv sync --frozen --dev") == 2
    assert "-W error::DeprecationWarning" in workflow
    for path in (
        "tests/test_project_env_security.py",
        "tests/test_file_lock.py",
        "tests/test_private_paths.py",
        "tests/test_artifact_security.py",
        "tests/test_safe_subprocess.py",
        "tests/test_shell_execution_security.py",
        "tests/test_shell_security_corpus.py",
        "tests/test_checkpoint_store_durability.py",
        "tests/test_recovery_durability_e2e.py",
        "tests/test_recovery_journal.py",
        "tests/memory/test_block_store.py",
        "tests/memory/test_reader_bounds.py",
        "tests/memory/test_retrieval.py",
    ):
        assert path in workflow
    assert "continue-on-error" not in workflow
    assert "-W ignore" not in workflow


def test_ci_keeps_docker_sandbox_local_gate_read_only():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("Docker Sandbox local identity gate (zero mutation)") == 2
    assert workflow.count("sandbox status") >= 2
    assert workflow.count('status["runtime_authorization"]["kind"] == "local"') == 2
    assert workflow.count('status["product_enablement"]["status"] == "blocked"') == 2
    assert workflow.count('data["data"]["network_performed"] is False') == 2
    assert workflow.count('data["data"]["mutation_performed"] is False') == 2
    assert "--sandbox run smoke" not in workflow
    assert "for command in status install repair" not in workflow
    assert "candidate_rejected" not in workflow
    assert "--real --managed" not in workflow
    assert "PICO_RUN_REAL_SRT" not in workflow
    assert "uv build --clear" in workflow
    assert "--install-smoke --offline-bundle-smoke" in workflow


def test_maintenance_scripts_start_and_show_help():
    for script in (
        "scripts/aggregate_docker_sandbox_release.py",
        "scripts/collect_resume_metrics.py",
        "scripts/aggregate_srt_feasibility.py",
        "scripts/evaluate.py",
        "scripts/run_large_scale_experiments.py",
        "scripts/run_provider_experiments.py",
        "scripts/srt_feasibility.py",
        "scripts/verify_distribution.py",
    ):
        result = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_distribution_verifier_freezes_archive_and_install_contract():
    verifier = Path("scripts/verify_distribution.py").read_text(encoding="utf-8")

    assert '"git", "ls-files", "--", "pico"' in verifier
    assert "sdist file mismatch" in verifier
    assert "wheel file mismatch" in verifier
    assert 'metadata.get_all("Requires-Dist") is None' in verifier
    assert '"command -v pico"' in verifier
    assert '_run(str(pico), "doctor", cwd=cwd, env=env)' in verifier
    assert '"sandbox",\n                "status"' in verifier
    assert 'prepared["runtime_authorization"]["kind"] == "local"' in verifier
    assert 'prepare.returncode in {0, 3}' in verifier
    assert 'status["data"]["network_performed"] is False' in verifier
    assert 'status["data"]["mutation_performed"] is False' in verifier
    assert "resources_after == resources_before" in verifier
    assert '"PYTHONHOME"' in verifier
    assert '"PYTHONPATH"' in verifier
    assert "Path(lifecycle.__file__).resolve().is_relative_to" in verifier
    assert "cwd=cwd, env=env" in verifier


def test_distribution_verifier_ignores_tracked_files_deleted_from_worktree(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PACKAGE_DATA_FILES", set())
    package = tmp_path / "pico"
    package.mkdir()
    (package / "present.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pico/present.py\npico/deleted.py\n",
    )

    tracked = module._tracked_package_files(tmp_path)

    assert "pico/present.py" in tracked
    assert "pico/deleted.py" not in tracked


def test_distribution_verifier_rejects_untracked_package_python(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PACKAGE_DATA_FILES", set())
    package = tmp_path / "pico"
    package.mkdir()
    (package / "tracked.py").write_text("", encoding="utf-8")
    (package / "untracked.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pico/tracked.py\n",
    )

    with pytest.raises(AssertionError, match="untracked package Python files"):
        module._tracked_package_files(tmp_path)


def test_distribution_verifier_rejects_untracked_package_data(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PACKAGE_DATA_FILES", {"pico/data.json"})
    package = tmp_path / "pico"
    package.mkdir()
    (package / "tracked.py").write_text("", encoding="utf-8")
    (package / "data.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pico/tracked.py\n",
    )

    with pytest.raises(AssertionError, match="untracked package data files"):
        module._tracked_package_files(tmp_path)


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

    assert args.max_new_tokens == provider_benchmark.DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS
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
