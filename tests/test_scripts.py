import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import benchmarks.evaluation.provider_benchmark as provider_benchmark
import pytest


def test_ci_tracks_and_uses_frozen_uv_lock():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert Path("uv.lock").is_file()
    assert "uv.lock" not in ignored
    assert 'version: "0.11.26"' in workflow
    triggers = workflow.split("permissions:", 1)[0]
    assert "  push:\n" in triggers
    assert "branches:" not in triggers
    assert "run: uv sync --frozen --dev" in workflow


def test_project_version_and_sandbox_build_inputs_are_locked():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    uv_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    pico_lock = next(item for item in uv_lock["package"] if item["name"] == "pico")
    image_lock = json.loads(
        Path("docker/sandbox/image-inputs.lock.json").read_text(encoding="utf-8")
    )

    assert project["version"] == pico_lock["version"] == "1.0.0"
    for filename, key in (
        ("pyproject.toml", "pyproject_sha256"),
        ("uv.lock", "uv_lock_sha256"),
    ):
        digest = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        assert image_lock["build_inputs"][key] == digest


def test_ci_actions_are_pinned_to_immutable_commits_with_version_comments():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    pins = {
        "actions/checkout": (
            "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "v7",
        ),
        "actions/setup-python": (
            "ece7cb06caefa5fff74198d8649806c4678c61a1",
            "v6",
        ),
        "astral-sh/setup-uv": (
            "fac544c07dec837d0ccb6301d7b5580bf5edae39",
            "v8.2.0",
        ),
        "actions/upload-artifact": (
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "v7",
        ),
    }
    uses = [line.strip() for line in workflow.splitlines() if "uses:" in line]
    assert uses
    assert all("@v" not in line for line in uses)
    for action, (commit, version) in pins.items():
        matches = [line for line in uses if f"uses: {action}@" in line]
        assert matches
        assert all(line == f"uses: {action}@{commit} # {version}" for line in matches)


def test_release_workflow_is_tag_bound_and_uses_trusted_publishing():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert "contents: write" in workflow
    assert "id-token: write" in workflow
    assert "environment: pypi" in workflow
    assert "uv sync --frozen --dev" in workflow
    assert "uv run pytest -q" in workflow
    assert "uv build --clear" in workflow
    assert (
        "scripts/release/verify_distribution.py --install-smoke --offline-bundle-smoke"
        in workflow
    )
    assert "uv publish --trusted-publishing always" in workflow
    assert "gh release create" in workflow
    assert 'test "${GITHUB_REF_NAME}" = "v${project_version}"' in workflow
    assert "secrets." not in workflow


def test_linux_ci_does_not_claim_the_darwin_performance_baseline():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "--suite core-functional" in workflow
    assert "--suite core-full" not in workflow


def test_ci_has_macos_security_and_durability_gate():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "runs-on: macos-latest" in workflow
    assert 'python-version: "3.12"' in workflow
    assert workflow.count("run: uv sync --frozen --dev") == 3
    linux_capability = workflow.split("linux-capability-evidence:", 1)[1]
    assert "- name: Install package\n        run: uv sync --frozen --dev" in (
        linux_capability
    )
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
    assert "product_enablement" not in workflow
    assert workflow.count('data["data"]["network_performed"] is False') == 2
    assert workflow.count('data["data"]["mutation_performed"] is False') == 2
    assert "--sandbox run smoke" not in workflow
    assert "for command in status install repair" not in workflow
    assert "candidate_rejected" not in workflow
    assert "--real --managed" not in workflow
    assert "PICO_RUN_REAL_SRT" not in workflow
    assert "uv build --clear" in workflow
    assert "scripts/release/verify_distribution.py" in workflow
    assert "--install-smoke --offline-bundle-smoke" in workflow


def test_maintenance_scripts_start_and_show_help():
    for script in (
        "scripts/evaluation/collect_resume_metrics.py",
        "scripts/evaluation/evaluate.py",
        "scripts/evaluation/run_large_scale_experiments.py",
        "scripts/evaluation/run_provider_experiments.py",
        "scripts/sandbox/build_image.py",
        "scripts/sandbox/verify_runtime.py",
        "scripts/release/verify_distribution.py",
    ):
        result = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_sandbox_evaluator_uses_the_local_runtime_verifier():
    evaluator = Path("scripts/evaluation/evaluate.py").read_text(encoding="utf-8")

    assert "scripts/sandbox/verify_runtime.py" in evaluator
    assert "docker_sandbox_release.py" not in evaluator


def test_distribution_verifier_freezes_archive_and_install_contract():
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    verifier = Path("scripts/release/verify_distribution.py").read_text(
        encoding="utf-8"
    )

    assert 'build-backend = "hatchling.build"' in project
    assert '[tool.hatch.build.targets.sdist]' in project
    assert 'packages = ["pico"]' in project
    assert "MANIFEST.in" not in project
    assert '"git", "ls-files", "--", "pico"' in verifier
    assert "sdist file mismatch" in verifier
    assert "wheel file mismatch" in verifier
    assert (
        'metadata.get_all("Requires-Dist") == EXPECTED_RUNTIME_REQUIREMENTS'
        in verifier
    )
    assert 'EXPECTED_RUNTIME_REQUIREMENTS = ["prompt-toolkit<4,>=3.0.52"]' in verifier
    assert 'metadata["License-Expression"] == "MIT"' in verifier
    assert 'installed_version == f"pico {PROJECT_VERSION}"' in verifier
    assert '"command -v pico"' in verifier
    assert '_run(str(pico), "doctor", cwd=cwd, env=env)' in verifier
    assert '"sandbox",\n                "status"' in verifier
    assert 'prepared["runtime_authorization"]["kind"] == "local"' in verifier
    assert "prepare.returncode in {0, 3}" in verifier
    assert 'status["data"]["network_performed"] is False' in verifier
    assert 'status["data"]["mutation_performed"] is False' in verifier
    assert "resources_after == resources_before" in verifier
    assert '"PYTHONHOME"' in verifier
    assert '"PYTHONPATH"' in verifier
    assert "pico.sandbox_lifecycle" in verifier
    assert "pico._sandbox_toolchain" in verifier
    assert "pico.sandbox.network_control" in verifier
    assert "pico.providers.fake" in verifier
    assert "pico = pico.cli.app:main" in verifier
    assert "offline_bundle_smoke" in verifier
    assert '"--no-index"' in verifier
    assert '"--offline"' in verifier
    assert "_locked_runtime_requirements" in verifier
    assert "import prompt_toolkit; import pico.tui.app" in verifier
    assert "cwd=cwd, env=env" in verifier
    assert 'PROJECT_VERSION = _PROJECT["version"]' in verifier
    assert 'PROJECT_VERSION = "' not in verifier


def test_distribution_verifier_ignores_tracked_files_deleted_from_worktree(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
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


def test_distribution_verifier_rejects_untracked_package_python(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
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


def test_distribution_verifier_rejects_untracked_package_data(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
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


def test_distribution_verifier_includes_all_product_packages(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PACKAGE_DATA_FILES = set()
    tracked = {
        "pico/__init__.py",
        "pico/runtime/application.py",
        "pico/agent/__init__.py",
        "pico/agent/loop.py",
    }

    runtime = module._runtime_package_files(tmp_path, tracked)

    assert runtime == tracked


def test_local_check_script_matches_ci_commands():
    script = Path("scripts/check.sh")

    assert script.exists()
    assert script.stat().st_mode & 0o111

    text = script.read_text()
    assert "uv lock --check" in text
    assert "uv run --frozen ruff check ." in text
    assert "uv run --frozen pytest -q" in text
    assert "scripts/evaluation/evaluate.py --suite core-functional" in text
    assert "scripts/release/verify_distribution.py" in text
    assert "--install-smoke" in text


def test_provider_experiment_defaults_allow_reasoning_budget():
    spec = importlib.util.spec_from_file_location(
        "run_provider_experiments_script",
        Path("scripts/evaluation/run_provider_experiments.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_arg_parser().parse_args(["--output-json", "out.json"])

    assert (
        args.max_output_tokens
        == provider_benchmark.DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS
    )
    assert args.max_output_tokens == 16_384


def test_provider_experiment_parser_uses_repo_root_and_rejects_provider_selector():
    spec = importlib.util.spec_from_file_location(
        "run_provider_experiments_script",
        Path("scripts/evaluation/run_provider_experiments.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    default_args = module.build_arg_parser().parse_args(["--output-json", "out.json"])
    selected_args = module.build_arg_parser().parse_args(
        ["--output-json", "out.json", "--repo-root", "/repo"]
    )

    assert default_args.repo_root == "."
    assert selected_args.repo_root == "/repo"
    with pytest.raises(SystemExit) as caught:
        module.build_arg_parser().parse_args(
            ["--output-json", "out.json", "--provider", "openai"]
        )
    assert caught.value.code == 2
