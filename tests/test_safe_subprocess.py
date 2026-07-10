import os
import stat
import subprocess

import pytest

from pico.safe_subprocess import (
    build_trusted_executables,
    discover_lexical_repo_root,
    run_hardened_git,
    run_hardened_rg,
)


def _executable(path, body="#!/bin/sh\nexit 0\n"):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_discover_lexical_repo_root_never_executes_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("git executed")),
    )

    assert discover_lexical_repo_root(child) == repo.resolve()


def test_discover_lexical_repo_root_accepts_git_file(tmp_path):
    repo = tmp_path / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    assert discover_lexical_repo_root(child) == repo.resolve()


def test_discover_lexical_repo_root_rejects_git_symlink_without_raw_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").symlink_to(tmp_path / "target")

    with pytest.raises(ValueError) as exc_info:
        discover_lexical_repo_root(repo)

    assert str(exc_info.value) == "unsafe .git symlink"
    assert str(tmp_path) not in str(exc_info.value)


def test_trusted_executables_ignore_relative_missing_and_workspace_path(tmp_path):
    fake = _executable(tmp_path / "git")

    trusted = build_trusted_executables(
        tmp_path,
        env={"PATH": f".:relative:{tmp_path / 'missing'}:{tmp_path}:/usr/bin"},
        names=("git",),
    )

    assert trusted.get("git") != str(fake)
    assert trusted.get("git", "").startswith("/")


def test_trusted_executables_skip_writable_path_entries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unsafe_bin = tmp_path / "unsafe-bin"
    unsafe_bin.mkdir()
    _executable(unsafe_bin / "git")
    unsafe_bin.chmod(unsafe_bin.stat().st_mode | stat.S_IWGRP)

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": str(unsafe_bin)},
        names=("git",),
    )

    assert "git" not in trusted


def test_trusted_executables_skip_writable_or_non_regular_executables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe_bin = tmp_path / "safe-bin"
    safe_bin.mkdir(mode=0o755)
    unsafe = _executable(safe_bin / "git")
    unsafe.chmod(unsafe.stat().st_mode | stat.S_IWOTH)
    (safe_bin / "rg").mkdir()

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": str(safe_bin)},
        names=("git", "rg"),
    )

    assert trusted == {}


def test_external_path_symlink_to_workspace_binary_is_rejected(tmp_path):
    external_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    external_bin.mkdir()
    fake = _executable(tmp_path / "git")
    (external_bin / "git").symlink_to(fake)

    trusted = build_trusted_executables(
        tmp_path,
        env={"PATH": str(external_bin)},
        names=("git",),
    )

    assert "git" not in trusted


def test_bad_path_entry_does_not_hide_later_trusted_executable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe_bin = tmp_path / "safe-bin"
    safe_bin.mkdir(mode=0o755)
    executable = _executable(safe_bin / "git")

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": f"{tmp_path / 'missing'}{os.pathsep}{safe_bin}"},
        names=("git",),
    )

    assert trusted == {"git": str(executable.resolve())}


def test_hardened_git_disables_repo_config_execution(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker-controlled"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "run-me"))
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-inherited")
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hardened_git("/usr/bin/git", ["status", "--short"], cwd=tmp_path)

    argv = captured["argv"]
    env = captured["kwargs"]["env"]
    assert argv[0] == "/usr/bin/git"
    assert argv[1] == "--no-pager"
    assert argv[-2:] == ["status", "--short"]
    assert "core.fsmonitor=false" in argv
    assert "core.hooksPath=/dev/null" in argv
    assert "diff.external=" in argv
    assert "credential.helper=" in argv
    assert "protocol.ext.allow=never" in argv
    assert "pager.status=false" in argv
    assert {name for name in env if name.startswith("GIT_")} == {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "UNRELATED_SECRET" not in env
    assert captured["kwargs"]["capture_output"] is True


def test_hardened_git_rejects_relative_executable_without_raw_path(tmp_path):
    with pytest.raises(ValueError) as exc_info:
        run_hardened_git("private/git", ["status"], cwd=tmp_path)

    assert "private/git" not in str(exc_info.value)


def test_hardened_rg_uses_fixed_config_and_minimal_environment(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(tmp_path / "malicious.conf"))
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-inherited")
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hardened_rg("/usr/bin/rg", ["needle", "."], cwd=tmp_path)

    assert captured["argv"] == ["/usr/bin/rg", "needle", "."]
    assert captured["kwargs"]["env"]["RIPGREP_CONFIG_PATH"] == os.devnull
    assert "UNRELATED_SECRET" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


@pytest.mark.parametrize("option", ["--pre", "--pre=cat", "--pre-glob", "--pre-glob=*.py"])
def test_hardened_rg_rejects_preprocessors(option, tmp_path, monkeypatch):
    def runner(*args, **kwargs):
        raise AssertionError("rg executed")

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(ValueError):
        run_hardened_rg("/usr/bin/rg", [option, "needle", "."], cwd=tmp_path)
