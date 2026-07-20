import importlib.util
import os
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


def test_project_version_is_locked():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    uv_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    pony_code_lock = next(
        item for item in uv_lock["package"] if item["name"] == "pony-code"
    )

    assert project["version"] == pony_code_lock["version"] == "1.0.0"


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
    assert "uv export --frozen --no-dev --no-emit-project" in workflow
    assert "uv pip install --refresh" in workflow
    assert "./scripts/check.sh" in workflow
    assert "./scripts/check.sh --release-dist" not in workflow
    assert "uv run pytest" not in workflow
    assert "uv build --offline --clear --no-create-gitignore --out-dir dist" in workflow
    assert 'UV_OFFLINE: "1"' in workflow
    assert "uv run --frozen python scripts/release/verify_distribution.py" in workflow
    assert "scripts/release/verify_distribution.py" in workflow
    assert "--dist-dir dist" in workflow
    assert "--install-smoke" in workflow
    assert "--offline-bundle-smoke" in workflow
    assert "sha256sum dist/*.whl dist/*.tar.gz" in workflow
    assert "uv publish --trusted-publishing always" in workflow
    assert workflow.index("./scripts/check.sh") < workflow.index("uv build")
    assert workflow.index("uv build") < workflow.index(
        "scripts/release/verify_distribution.py"
    )
    assert workflow.index("scripts/release/verify_distribution.py") < workflow.index(
        "sha256sum"
    )
    assert workflow.count("uv build") == 1
    assert workflow.count("scripts/release/verify_distribution.py") == 1
    assert "gh release create" in workflow
    assert 'test "${GITHUB_REF_NAME}" = "v${project_version}"' in workflow
    assert "secrets." not in workflow


def test_linux_ci_uses_the_single_exact_head_gate():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    linux, _macos = workflow.split("macos-focused:", 1)

    assert linux.count("./scripts/check.sh") == 1
    assert linux.count("uv run --frozen pytest -q tests") == 1
    assert "scripts/evaluation/evaluate.py" not in linux
    assert "scripts/release/verify_distribution.py" not in linux
    assert "uv build" not in linux


def test_ci_has_macos_security_and_durability_gate():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "runs-on: macos-latest" in workflow
    assert 'python-version: "3.12"' in workflow
    assert workflow.count("uv sync --frozen --dev") == 2
    assert "uv export --frozen --no-dev --no-emit-project" in workflow
    assert "uv pip install --refresh" in workflow
    assert "sandbox-contract" not in workflow
    assert "linux-capability-evidence" not in workflow
    assert "-W error::DeprecationWarning" in workflow
    for path in (
        "tests/test_project_env_security.py",
        "tests/test_file_lock.py",
        "tests/test_private_paths.py",
        "tests/test_artifact_security.py",
        "tests/test_safe_subprocess.py",
        "tests/test_shell_execution_security.py",
        "tests/test_shell_security_corpus.py",
        "tests/memory/test_block_store.py",
        "tests/memory/test_reader_bounds.py",
        "tests/memory/test_retrieval.py",
    ):
        assert path in workflow
    assert "continue-on-error" not in workflow
    assert "-W ignore" not in workflow


def test_maintenance_scripts_start_and_show_help():
    for script in (
        "scripts/evaluation/collect_resume_metrics.py",
        "scripts/evaluation/evaluate.py",
        "scripts/evaluation/run_large_scale_experiments.py",
        "scripts/evaluation/run_provider_experiments.py",
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


def test_distribution_verifier_freezes_archive_and_install_contract():
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    verifier = Path("scripts/release/verify_distribution.py").read_text(
        encoding="utf-8"
    )

    assert 'build-backend = "hatchling.build"' in project
    assert '[tool.hatch.build.targets.sdist]' in project
    assert 'packages = ["pony"]' in project
    assert "MANIFEST.in" not in project
    assert '"git", "ls-files", "--", "pony"' in verifier
    assert "sdist file mismatch" in verifier
    assert "wheel file mismatch" in verifier
    assert (
        'metadata.get_all("Requires-Dist") == EXPECTED_RUNTIME_REQUIREMENTS'
        in verifier
    )
    assert 'EXPECTED_RUNTIME_REQUIREMENTS = ["prompt-toolkit<4,>=3.0.52"]' in verifier
    assert 'metadata["License-Expression"] == "MIT"' in verifier
    assert 'installed_version == f"pony {PROJECT_VERSION}"' in verifier
    assert '"command -v pony"' in verifier
    assert '_run(str(pony), "doctor", cwd=cwd, env=env)' in verifier
    assert '"PYTHONHOME"' in verifier
    assert '"PYTHONPATH"' in verifier
    assert "pony.providers.fake" in verifier
    assert "pony = pony.cli.app:main" in verifier
    assert "offline_bundle_smoke" in verifier
    assert '"--no-index"' in verifier
    assert '"--offline"' in verifier
    assert "_locked_runtime_requirements" in verifier
    assert "import prompt_toolkit; import pony.tui.app" in verifier
    assert "cwd=cwd, env=env" in verifier
    assert 'PROJECT_VERSION = _PROJECT["version"]' in verifier
    assert 'PROJECT_VERSION = "' not in verifier
    assert 'EXPECTED_REQUIRES_PYTHON = "<3.13,>=3.11"' in verifier
    assert 'metadata["Requires-Python"] == EXPECTED_REQUIRES_PYTHON' in verifier


def test_distribution_verifier_ignores_tracked_files_deleted_from_worktree(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "pony"
    package.mkdir()
    (package / "present.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pony/present.py\npony/deleted.py\n",
    )

    tracked = module._tracked_package_files(tmp_path)

    assert "pony/present.py" in tracked
    assert "pony/deleted.py" not in tracked


def test_distribution_verifier_rejects_untracked_package_python(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "pony"
    package.mkdir()
    (package / "tracked.py").write_text("", encoding="utf-8")
    (package / "untracked.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pony/tracked.py\n",
    )

    with pytest.raises(AssertionError, match="untracked package Python files"):
        module._tracked_package_files(tmp_path)


def test_distribution_verifier_rejects_tracked_package_data(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "pony"
    package.mkdir()
    (package / "tracked.py").write_text("", encoding="utf-8")
    (package / "data.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: "pony/tracked.py\npony/data.json\n",
    )

    with pytest.raises(AssertionError, match="unexpected tracked package files"):
        module._tracked_package_files(tmp_path)


def test_distribution_verifier_includes_all_product_packages(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "verify_distribution_script",
        Path("scripts/release/verify_distribution.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tracked = {
        "pony/__init__.py",
        "pony/runtime/application.py",
        "pony/agent/__init__.py",
        "pony/agent/loop.py",
    }

    runtime = module._runtime_package_files(tmp_path, tracked)

    assert runtime == tracked


def test_local_check_script_runs_each_full_gate_once_on_a_clean_exact_head():
    script = Path("scripts/check.sh")

    assert script.exists()
    assert script.stat().st_mode & 0o111

    text = script.read_text()
    assert "uv lock --check" in text
    assert "UV_OFFLINE=1" in text
    assert text.count("uv run --frozen ruff check .") == 1
    assert text.count("uv run --frozen pytest") == 1
    assert "tests benchmarks/live_e2e/tests/test_assertions.py" in text
    assert "scripts/evaluation/evaluate.py" in text
    assert "--suite core-functional" in text
    assert '--output-dir "$tmp_dir/eval"' in text
    assert text.count("uv build") == 1
    assert "uv build --offline --clear --no-create-gitignore --out-dir" in text
    assert text.count("scripts/release/verify_distribution.py") == 1
    assert '--dist-dir "$dist_dir"' in text
    assert 'tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/pony-check.XXXXXX")' in text
    assert "--release-dist" not in text
    assert "--dist-dir PATH" not in text
    assert "--install-smoke" in text
    assert "git status --porcelain --untracked-files=all" in text
    assert "git rev-parse HEAD" in text
    assert "checking clean exact HEAD $start_head" in text
    assert "verified clean exact HEAD $start_head" in text
    assert "trap cleanup 0" in text
    assert "trap 'exit 129' 1" in text
    assert "trap 'exit 130' 2" in text
    assert "trap 'exit 143' 15" in text


def _check_fixture(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = repo / "bin"
    check_tmp = tmp_path / "check-tmp"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    check_tmp.mkdir()
    check = scripts / "check.sh"
    check.write_text(Path("scripts/check.sh").read_text(encoding="utf-8"))
    check.chmod(0o755)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$PONY_FAKE_UV_MODE" in\n'
        "  fail) exit 7 ;;\n"
        '  term) kill -TERM "$PPID"; sleep 0.1 ;;\n'
        "esac\n"
        'if [ "$1" = "build" ]; then\n'
        "  while [ \"$#\" -gt 0 ]; do\n"
        '    if [ "$1" = "--out-dir" ]; then shift; out_dir=$1; fi\n'
        "    shift\n"
        "  done\n"
        '  mkdir -p "$out_dir"\n'
        '  : > "$out_dir/pony_code-1.0.0.tar.gz"\n'
        '  : > "$out_dir/pony_code-1.0.0-py3-none-any.whl"\n'
        "fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Pony Test",
            "-c",
            "user.email=pony@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(fake_bin), env["PATH"]))
    env["TMPDIR"] = str(check_tmp)
    return repo, check, env


def _run_check(repo, check, env, *args, mode="success"):
    run_env = env.copy()
    run_env["PONY_FAKE_UV_MODE"] = mode
    return subprocess.run(
        [str(check), *args],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(("mode", "expected_status"), (("fail", 7), ("term", 143)))
def test_local_check_cleanup_preserves_failure_status(tmp_path, mode, expected_status):
    repo, check, env = _check_fixture(tmp_path)

    result = _run_check(repo, check, env, mode=mode)

    assert result.returncode == expected_status, result.stderr
    assert not (repo / "dist").exists()
    assert list(Path(env["TMPDIR"]).glob("pony-check.*")) == []


def test_local_check_rejects_release_dist_argument(tmp_path):
    repo, check, env = _check_fixture(tmp_path)

    result = _run_check(repo, check, env, "--release-dist")

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert list(Path(env["TMPDIR"]).glob("pony-check.*")) == []


def test_local_check_keeps_distributions_in_temporary_directory(tmp_path):
    repo, check, env = _check_fixture(tmp_path)

    result = _run_check(repo, check, env)

    assert result.returncode == 0, result.stderr
    assert not (repo / "dist").exists()
    assert list(Path(env["TMPDIR"]).glob("pony-check.*")) == []


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
