import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from pico import safe_subprocess as safe_subprocess_module
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


def _real_git():
    executable = shutil.which("git")
    assert executable
    return str(Path(executable).resolve())


def _init_git_repo(path):
    git = _real_git()
    subprocess.run([git, "init", "-q"], cwd=path, check=True)
    subprocess.run(
        [git, "config", "user.email", "tests@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        [git, "config", "user.name", "Pico Tests"],
        cwd=path,
        check=True,
    )
    return git


def _commit_readme(path):
    git = _init_git_repo(path)
    (path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run([git, "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        [git, "commit", "-q", "-m", "initial"],
        cwd=path,
        check=True,
    )
    return git


def _linked_worktree(tmp_path):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    git = _commit_readme(repo)
    subprocess.run(
        [git, "worktree", "add", "-q", str(linked)],
        cwd=repo,
        check=True,
    )
    return git, repo, linked


def _absorbed_submodule(tmp_path):
    source = tmp_path / "source"
    main = tmp_path / "main"
    source.mkdir()
    main.mkdir()
    git = _commit_readme(source)
    _commit_readme(main)
    subprocess.run(
        [
            git,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "child",
        ],
        cwd=main,
        check=True,
    )
    return git, main / "child"


def _gitfile_target(marker):
    value = marker.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    return (marker.parent / value).resolve()


def _assert_gitfile_rejected_before_git(monkeypatch, cwd, args=("status", "--short")):
    calls = []

    def fail_if_called(*call_args, **call_kwargs):
        calls.append((call_args, call_kwargs))
        raise AssertionError("git executed before gitfile validation")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    with pytest.raises(ValueError, match="unsafe git repository"):
        run_hardened_git(_real_git(), args, cwd=cwd, text=True)

    assert calls == []


def _fifo_gitfile_probe(executable, cwd, marker_to_replace, connection):
    from pico import safe_subprocess

    swapped = False
    if marker_to_replace is not None:
        marker = Path(marker_to_replace)
        marker.unlink()
        os.mkfifo(marker)
        swapped = True

    safe_subprocess.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("git executed before FIFO metadata rejection")
    )
    try:
        safe_subprocess.run_hardened_git(
            executable,
            ["status", "--short"],
            cwd=cwd,
        )
    except ValueError:
        connection.send(("rejected", swapped))
    except BaseException as exc:
        connection.send((type(exc).__name__, swapped))
    else:
        connection.send(("accepted", swapped))
    finally:
        connection.close()


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


def test_external_path_hardlink_to_workspace_binary_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_bin = tmp_path / "external-bin"
    external_bin.mkdir(mode=0o755)
    controlled = _executable(workspace / "git")
    os.link(controlled, external_bin / "git")

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": str(external_bin)},
        names=("git",),
    )

    assert "git" not in trusted


def test_executable_parent_swap_is_rejected_by_anchored_traversal(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controlled = _executable(workspace / "git")
    safe_bin = tmp_path / "safe-bin"
    safe_bin.mkdir()
    candidate = _executable(safe_bin / "git")
    moved = tmp_path / "safe-bin-original"
    real_open = safe_subprocess_module.os.open
    swapped = False

    def swap_parent(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fspath(path) == safe_bin.name:
            safe_bin.rename(moved)
            safe_bin.symlink_to(workspace, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_subprocess_module.os, "open", swap_parent)

    with pytest.raises((OSError, ValueError)):
        safe_subprocess_module._verified_executable_identity(candidate)

    assert controlled.exists()


def test_mutable_git_is_rejected_before_any_probe_executes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    safe_bin = tmp_path / "safe-bin"
    safe_bin.mkdir()
    source = _executable(
        safe_bin / "git",
        f"#!/bin/sh\nexec {_real_git()} \"$@\"\n",
    )
    trusted = build_trusted_executables(
        repo,
        env={"PATH": str(safe_bin)},
        names=("git",),
    )
    calls = []

    def record_probe(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("mutable executable must not run")

    monkeypatch.setattr(
        safe_subprocess_module.subprocess,
        "run",
        record_probe,
    )

    with pytest.raises(ValueError, match="mutable trusted executable"):
        run_hardened_git(str(source), ["status", "--short"], cwd=repo)

    assert trusted == {}
    assert source.exists()
    assert calls == []


def test_owner_can_chmod_nominally_read_only_executable_and_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner_bin = tmp_path / "owner-bin"
    owner_bin.mkdir()
    executable = _executable(owner_bin / "git")
    executable.chmod(0o555)
    owner_bin.chmod(0o555)

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": str(owner_bin)},
        names=("git",),
    )

    assert trusted == {}


def test_bad_path_entry_does_not_hide_later_trusted_executable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": f"{tmp_path / 'missing'}{os.pathsep}/usr/bin"},
        names=("git",),
    )

    assert trusted == {"git": "/usr/bin/git"}


@pytest.mark.parametrize("path_kind", ["empty", "relative", "workspace", "writable"])
def test_empty_safe_path_never_calls_which(path_kind, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_cwd = tmp_path / "process-cwd"
    process_cwd.mkdir(mode=0o755)
    fake = _executable(process_cwd / "git")
    monkeypatch.chdir(process_cwd)
    if path_kind == "empty":
        path_value = ""
    elif path_kind == "relative":
        path_value = "relative"
    elif path_kind == "workspace":
        path_value = str(workspace)
    else:
        process_cwd.chmod(process_cwd.stat().st_mode | stat.S_IWGRP)
        path_value = str(process_cwd)
    calls = []

    def cwd_fallback(name, *, path):
        calls.append((name, path))
        return str(fake)

    monkeypatch.setattr("pico.safe_subprocess.shutil.which", cwd_fallback)

    trusted = build_trusted_executables(
        workspace,
        env={"PATH": path_value},
        names=("git",),
    )

    assert trusted == {}
    assert calls == []


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
    assert argv[2] == "--no-optional-locks"
    assert argv[-2:] == ["status", "--short"]
    assert "core.fsmonitor=false" in argv
    assert "core.hooksPath=/dev/null" in argv
    assert "diff.external=" in argv
    assert "credential.helper=" in argv
    assert "protocol.ext.allow=never" in argv
    assert "pager.status=false" in argv
    assert "core.askPass=" in argv
    assert "alias.status=" in argv
    assert {name for name in env if name.startswith("GIT_")} == {
        "GIT_ALLOW_PROTOCOL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
    }
    assert env["GIT_ALLOW_PROTOCOL"] == "git:http:https:ssh"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "UNRELATED_SECRET" not in env
    assert captured["kwargs"]["capture_output"] is True


def test_hardened_git_blocks_real_repo_local_clean_filter(tmp_path):
    git = _commit_readme(tmp_path)
    marker = tmp_path / "filter-ran"
    subprocess.run(
        [git, "config", "filter.evil.clean", f"touch {marker}"],
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["status", "--short"], cwd=tmp_path)

    assert not marker.exists()


def test_hardened_git_blocks_filter_from_included_config(tmp_path):
    git = _commit_readme(tmp_path)
    marker = tmp_path / "included-filter-ran"
    included = tmp_path / "included.gitconfig"
    included.write_text(
        f'[filter "evil"]\n\tclean = touch {marker}\n',
        encoding="utf-8",
    )
    subprocess.run(
        [git, "config", "include.path", str(included)],
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["status", "--short"], cwd=tmp_path)

    assert not marker.exists()


def test_hardened_git_blocks_mixed_case_filter_from_worktree_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    linked = tmp_path / "linked"
    git = _commit_readme(repo)
    subprocess.run(
        [git, "worktree", "add", "-q", str(linked)],
        cwd=repo,
        check=True,
    )
    assert run_hardened_git(git, ["status", "--short"], cwd=linked).returncode == 0
    marker = tmp_path / "worktree-filter-ran"
    subprocess.run(
        [git, "config", "extensions.worktreeConfig", "true"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        [git, "config", "--worktree", "FiLtEr.EvIl.ClEaN", f"touch {marker}"],
        cwd=linked,
        check=True,
    )
    assert (linked / ".git").is_file()

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["status", "--short"], cwd=linked)

    assert not marker.exists()


@pytest.mark.parametrize(
    "key",
    [
        "browser.evil.cmd",
        "core.alternateRefsCommand",
        "core.editor",
        "core.sshCommand",
        "core.gitProxy",
        "core.askPass",
        "core.worktree",
        "difftool.evil.cmd",
        "difftool.evil.path",
        "gc.recentObjectsHook",
        "gpg.program",
        "gpg.ssh.defaultKeyCommand",
        "guitool.evil.cmd",
        "interactive.diffFilter",
        "man.evil.cmd",
        "merge.evil.driver",
        "mergetool.evil.cmd",
        "remote.origin.proxy",
        "remote.origin.uploadpack",
        "remote.origin.receivepack",
        "remote.origin.vcs",
        "credential.https://example.com.helper",
        "sendemail.headerCmd",
        "sendemail.smtpServer",
        "sequence.editor",
        "trailer.issue.command",
        "uploadpack.packObjectsHook",
    ],
)
def test_hardened_git_blocks_executable_config_key(tmp_path, key):
    git = _commit_readme(tmp_path)
    subprocess.run(
        [git, "config", key, "dangerous-helper"],
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["fetch", "origin"], cwd=tmp_path)


def test_hardened_git_allows_neutralized_exact_credential_helper(tmp_path):
    git = _commit_readme(tmp_path)
    marker = tmp_path / "credential-helper-ran"
    subprocess.run(
        [git, "config", "credential.helper", f"!touch {marker}"],
        cwd=tmp_path,
        check=True,
    )

    result = run_hardened_git(
        git,
        ["status", "--short"],
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_hardened_git_blocks_filter_config_in_bare_repo(tmp_path):
    git = _real_git()
    bare = tmp_path / "repo.git"
    subprocess.run([git, "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(
        [git, "config", "filter.evil.clean", "dangerous-helper"],
        cwd=bare,
        check=True,
    )

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["status", "--short"], cwd=bare)


def test_hardened_git_fails_closed_for_bare_repo_config_symlink(tmp_path):
    git = _real_git()
    bare = tmp_path / "repo.git"
    subprocess.run([git, "init", "-q", "--bare", str(bare)], check=True)
    marker = tmp_path / "bare-ssh-command-ran"
    ssh_command = _executable(
        tmp_path / "bare-ssh-command",
        f"#!/bin/sh\ntouch {marker}\nexit 1\n",
    )
    subprocess.run(
        [git, "config", "core.sshCommand", str(ssh_command)],
        cwd=bare,
        check=True,
    )
    subprocess.run(
        [git, "remote", "add", "origin", "ssh://example.invalid/repo"],
        cwd=bare,
        check=True,
    )
    external_config = tmp_path / "bare-config"
    (bare / "config").replace(external_config)
    (bare / "config").symlink_to(external_config)

    with pytest.raises(ValueError, match="unsafe git repository"):
        run_hardened_git(git, ["fetch", "origin"], cwd=bare)

    assert not marker.exists()


def test_hardened_git_allows_absorbed_submodule_core_worktree(tmp_path):
    git, child = _absorbed_submodule(tmp_path)
    assert (child / ".git").is_file()
    core_worktree = subprocess.run(
        [git, "config", "--local", "--get", "core.worktree"],
        cwd=child,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert core_worktree

    result = run_hardened_git(
        git,
        ["status", "--short"],
        cwd=child,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_hardened_git_allows_absorbed_submodule_in_linked_worktree(tmp_path):
    git, _, linked = _linked_worktree(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _commit_readme(source)
    subprocess.run(
        [
            git,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "child",
        ],
        cwd=linked,
        check=True,
    )
    child = linked / "child"

    result = run_hardened_git(
        git,
        ["status", "--short"],
        cwd=child,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_hardened_git_allows_nested_absorbed_submodule(tmp_path):
    leaf_source = tmp_path / "leaf-source"
    parent_source = tmp_path / "parent-source"
    main = tmp_path / "main"
    leaf_source.mkdir()
    parent_source.mkdir()
    main.mkdir()
    git = _commit_readme(leaf_source)
    _commit_readme(parent_source)
    subprocess.run(
        [
            git,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(leaf_source),
            "nested",
        ],
        cwd=parent_source,
        check=True,
    )
    subprocess.run(
        [git, "commit", "-q", "-m", "add nested submodule"],
        cwd=parent_source,
        check=True,
    )
    _commit_readme(main)
    subprocess.run(
        [
            git,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(parent_source),
            "parent",
        ],
        cwd=main,
        check=True,
    )
    subprocess.run(
        [
            git,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        ],
        cwd=main,
        check=True,
        capture_output=True,
    )
    nested = main / "parent" / "nested"
    assert _gitfile_target(nested / ".git") == (
        main / ".git" / "modules" / "parent" / "modules" / "nested"
    ).resolve()

    result = run_hardened_git(
        git,
        ["status", "--short"],
        cwd=nested,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_hardened_git_allows_linked_worktree(tmp_path):
    git, _, linked = _linked_worktree(tmp_path)

    result = run_hardened_git(
        git,
        ["status", "--short"],
        cwd=linked,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_hardened_git_rejects_separate_git_dir_before_git(
    tmp_path,
    monkeypatch,
):
    git = _real_git()
    workspace = tmp_path / "workspace"
    git_dir = tmp_path / "separate.git"
    workspace.mkdir()
    subprocess.run(
        [git, "init", "-q", f"--separate-git-dir={git_dir}", str(workspace)],
        check=True,
    )
    assert (workspace / ".git").is_file()

    _assert_gitfile_rejected_before_git(monkeypatch, workspace)


def test_hardened_git_external_gitdir_cannot_read_head_env(
    tmp_path,
    monkeypatch,
):
    secret = "external-object-secret"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    git = _commit_readme(outside)
    (outside / ".env").write_text(secret, encoding="utf-8")
    subprocess.run([git, "add", ".env"], cwd=outside, check=True)
    subprocess.run(
        [git, "commit", "-q", "-m", "add external secret"],
        cwd=outside,
        check=True,
    )
    (workspace / ".git").write_text(
        f"gitdir: {outside / '.git'}\n",
        encoding="utf-8",
    )
    exposed = subprocess.run(
        [git, "show", "HEAD:.env"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert exposed.stdout.strip() == secret

    _assert_gitfile_rejected_before_git(
        monkeypatch,
        workspace,
        args=("show", "HEAD:.env"),
    )


def test_hardened_git_rejects_forged_absorbed_external_gitdir(
    tmp_path,
    monkeypatch,
):
    secret = "forged-absorbed-secret"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    git = _commit_readme(outside)
    (outside / ".env").write_text(secret, encoding="utf-8")
    subprocess.run([git, "add", ".env"], cwd=outside, check=True)
    subprocess.run(
        [git, "commit", "-q", "-m", "add external secret"],
        cwd=outside,
        check=True,
    )
    subprocess.run(
        [git, "config", "core.worktree", str(workspace)],
        cwd=outside,
        check=True,
    )
    (workspace / ".git").write_text(
        f"gitdir: {outside / '.git'}\n",
        encoding="utf-8",
    )
    exposed = subprocess.run(
        [git, "show", "HEAD:.env"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert exposed.stdout.strip() == secret

    _assert_gitfile_rejected_before_git(
        monkeypatch,
        workspace,
        args=("show", "HEAD:.env"),
    )


@pytest.mark.parametrize(
    "marker_content",
    [
        b"not-a-gitdir\n",
        b"gitdir: \n",
        b"gitdir: target\nextra\n",
        b"gitdir: " + b"x" * 70_000,
    ],
    ids=("wrong-prefix", "empty", "multiple-lines", "oversized"),
)
def test_hardened_git_rejects_malformed_gitfile_before_git(
    marker_content,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_bytes(marker_content)

    _assert_gitfile_rejected_before_git(monkeypatch, workspace)


def test_hardened_git_rejects_symlinked_gitfile_target_before_git(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    target_link = tmp_path / "target.git"
    workspace.mkdir()
    outside.mkdir()
    (outside / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    target_link.symlink_to(outside, target_is_directory=True)
    (workspace / ".git").write_text(
        f"gitdir: {target_link}\n",
        encoding="utf-8",
    )

    _assert_gitfile_rejected_before_git(monkeypatch, workspace)


@pytest.mark.parametrize(
    "mutation",
    [
        "mismatched-backlink",
        "malformed-backlink",
        "oversized-backlink",
        "symlinked-backlink",
        "mismatched-commondir",
        "oversized-commondir",
        "symlinked-commondir",
        "symlinked-common-config",
    ],
)
def test_hardened_git_rejects_invalid_linked_worktree_metadata_before_git(
    mutation,
    tmp_path,
    monkeypatch,
):
    _, _, linked = _linked_worktree(tmp_path)
    marker = linked / ".git"
    target = _gitfile_target(marker)
    backlink = target / "gitdir"
    commondir = target / "commondir"

    if mutation == "mismatched-backlink":
        other = tmp_path / "other-marker"
        other.write_text("gitdir: elsewhere\n", encoding="utf-8")
        backlink.write_text(f"{other}\n", encoding="utf-8")
    elif mutation == "malformed-backlink":
        backlink.write_text(f"{marker}\nextra\n", encoding="utf-8")
    elif mutation == "oversized-backlink":
        backlink.write_bytes(b"x" * 70_000)
    elif mutation == "symlinked-backlink":
        saved = tmp_path / "saved-backlink"
        backlink.replace(saved)
        backlink.symlink_to(saved)
    elif mutation == "mismatched-commondir":
        other = tmp_path / "other-common"
        other.mkdir()
        (other / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        commondir.write_text(f"{other}\n", encoding="utf-8")
    elif mutation == "oversized-commondir":
        commondir.write_bytes(b"x" * 70_000)
    elif mutation == "symlinked-commondir":
        saved = tmp_path / "saved-commondir"
        commondir.replace(saved)
        commondir.symlink_to(saved)
    else:
        common = (target / commondir.read_text(encoding="utf-8").strip()).resolve()
        config = common / "config"
        saved = tmp_path / "saved-common-config"
        config.replace(saved)
        config.symlink_to(saved)

    _assert_gitfile_rejected_before_git(monkeypatch, linked)


def test_hardened_git_rejects_absorbed_worktree_config_extension_before_git(
    tmp_path,
    monkeypatch,
):
    git, child = _absorbed_submodule(tmp_path)
    target = _gitfile_target(child / ".git")
    config = target / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[extensions]\n\tworktreeConfig = true\n",
        encoding="utf-8",
    )
    (target / "config.worktree").write_text(
        "[pico]\n\tprobe = read-from-config-worktree\n",
        encoding="utf-8",
    )
    observed = subprocess.run(
        [git, "config", "--worktree", "--get", "pico.probe"],
        cwd=child,
        check=True,
        capture_output=True,
        text=True,
    )
    assert observed.stdout.strip() == "read-from-config-worktree"

    _assert_gitfile_rejected_before_git(monkeypatch, child)


@pytest.mark.parametrize(
    "mutation",
    [
        "symlink",
        "oversized",
        "include",
        "alias",
        "helper",
        "hook",
        "duplicate-worktree",
    ],
)
def test_hardened_git_rejects_invalid_absorbed_submodule_config_before_git(
    mutation,
    tmp_path,
    monkeypatch,
):
    _, child = _absorbed_submodule(tmp_path)
    target = _gitfile_target(child / ".git")
    config = target / "config"

    if mutation == "symlink":
        saved = tmp_path / "saved-submodule-config"
        config.replace(saved)
        config.symlink_to(saved)
    elif mutation == "oversized":
        config.write_bytes(config.read_bytes() + b"#" + b"x" * 70_000)
    elif mutation == "include":
        included = tmp_path / "included-config"
        included.write_text("[core]\n\tbare = false\n", encoding="utf-8")
        config.write_text(
            config.read_text(encoding="utf-8")
            + f"\n[include]\n\tpath = {included}\n",
            encoding="utf-8",
        )
    elif mutation == "alias":
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[alias]\n\tstatus = !dangerous-helper\n",
            encoding="utf-8",
        )
    elif mutation == "helper":
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[credential]\n\thelper = dangerous-helper\n",
            encoding="utf-8",
        )
    elif mutation == "hook":
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[core]\n\thooksPath = dangerous-hooks\n",
            encoding="utf-8",
        )
    else:
        config.write_text(
            config.read_text(encoding="utf-8")
            + f"\n[core]\n\tworktree = {child}\n",
            encoding="utf-8",
        )

    _assert_gitfile_rejected_before_git(monkeypatch, child)


@pytest.mark.parametrize("metadata", ["marker", "config"])
def test_hardened_git_rejects_hardlinked_gitfile_metadata_before_git(
    metadata,
    tmp_path,
    monkeypatch,
):
    _, child = _absorbed_submodule(tmp_path)
    marker = child / ".git"
    target = _gitfile_target(marker)
    path = marker if metadata == "marker" else target / "config"
    saved = tmp_path / f"saved-{metadata}"
    path.replace(saved)
    os.link(saved, path)
    assert path.stat().st_nlink == 2

    _assert_gitfile_rejected_before_git(monkeypatch, child)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="real FIFO probe requires POSIX FIFO support",
)
@pytest.mark.parametrize("metadata", ["marker", "linked-backlink"])
def test_hardened_git_rejects_fifo_metadata_without_blocking(metadata, tmp_path):
    marker_to_replace = None
    if metadata == "marker":
        _, cwd = _absorbed_submodule(tmp_path)
        marker_to_replace = cwd / ".git"
    else:
        _, _, cwd = _linked_worktree(tmp_path)
        target = _gitfile_target(cwd / ".git")
        backlink = target / "gitdir"
        backlink.unlink()
        os.mkfifo(backlink)

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_fifo_gitfile_probe,
        args=(_real_git(), cwd, marker_to_replace, sender),
    )
    process.start()
    sender.close()
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail(f"{metadata} FIFO blocked metadata validation")

    assert process.exitcode == 0
    assert receiver.poll()
    assert receiver.recv() == ("rejected", metadata == "marker")
    receiver.close()


def test_hardened_git_marker_read_is_anchored_against_parent_swap(
    tmp_path,
    monkeypatch,
):
    _, child = _absorbed_submodule(tmp_path)
    marker = child / ".git"
    valid_marker = marker.read_bytes()
    marker.write_text("not-a-gitdir\n", encoding="utf-8")
    replacement = child.parent / "replacement-child"
    replacement.mkdir()
    (replacement / ".git").write_bytes(valid_marker)
    displaced = child.parent / "displaced-child"
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        raw_path = os.fsdecode(path)
        opening_marker = (dir_fd is None and Path(raw_path) == marker) or (
            dir_fd is not None and raw_path == ".git"
        )
        if opening_marker and not swapped:
            child.replace(displaced)
            replacement.replace(child)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)

    _assert_gitfile_rejected_before_git(monkeypatch, child)
    assert swapped is True


def test_hardened_git_marker_kind_is_anchored_against_parent_swap(
    tmp_path,
    monkeypatch,
):
    _, child = _absorbed_submodule(tmp_path)
    marker = child / ".git"
    valid_marker = marker.read_bytes()
    marker.write_text("not-a-gitdir\n", encoding="utf-8")
    replacement = child.parent / "replacement-child"
    replacement.mkdir()
    (replacement / ".git").write_bytes(valid_marker)
    displaced = child.parent / "displaced-child"
    real_stat = os.stat
    swapped = False

    def racing_stat(path, *args, dir_fd=None, follow_symlinks=True):
        nonlocal swapped
        raw_path = os.fsdecode(path)
        checking_marker = (dir_fd is None and Path(raw_path) == marker) or (
            dir_fd is not None and raw_path == ".git"
        )
        if checking_marker and not swapped:
            child.replace(displaced)
            replacement.replace(child)
            swapped = True
        return real_stat(
            path,
            *args,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "stat", racing_stat)

    _assert_gitfile_rejected_before_git(monkeypatch, child)
    assert swapped is True


def test_hardened_git_blocks_gitfile_core_worktree_escape(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    git = _commit_readme(outside)
    subprocess.run(
        [git, "config", "core.worktree", str(outside)],
        cwd=outside,
        check=True,
    )
    (workspace / ".git").write_text(
        f"gitdir: {outside / '.git'}\n",
        encoding="utf-8",
    )

    _assert_gitfile_rejected_before_git(monkeypatch, workspace)


@pytest.mark.parametrize(
    "args",
    [
        ["rev-parse", "--show-toplevel"],
        ["rev-parse", "--is-inside-work-tree"],
        ["show", "HEAD:README.md"],
    ],
)
def test_hardened_git_safe_exact_query_rejects_git_symlink(tmp_path, args):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _commit_readme(workspace)
    _commit_readme(outside)
    saved_git = tmp_path / "workspace-git"
    (workspace / ".git").replace(saved_git)
    (workspace / ".git").symlink_to(outside / ".git")

    with pytest.raises(ValueError, match=r"unsafe \.git symlink"):
        run_hardened_git(_real_git(), args, cwd=workspace, text=True)


def test_hardened_git_config_probe_nonzero_fails_closed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3, stdout=b"", stderr=b"bad")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git("/usr/bin/git", ["status"], cwd=tmp_path)

    assert len(calls) == 1
    assert "config" in calls[0]


def test_hardened_git_config_probe_timeout_fails_closed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        run_hardened_git("/usr/bin/git", ["status"], cwd=tmp_path)

    assert len(calls) == 1
    assert "config" in calls[0]


def test_hardened_git_blocks_index_gitlink(tmp_path):
    git = _commit_readme(tmp_path)
    head = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            git,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},modules/child",
        ],
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git(git, ["status", "--short"], cwd=tmp_path)


def test_hardened_git_rejects_malformed_ls_files_probe_output(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if "ls-files" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=b"not-an-index-record\x00",
                stderr=b"",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="unsafe git repository config"):
        run_hardened_git("/usr/bin/git", ["status"], cwd=tmp_path)

    assert len(calls) == 2


def test_hardened_git_non_repo_status_reaches_git(tmp_path):
    result = run_hardened_git(
        _real_git(),
        ["status", "--short"],
        cwd=tmp_path,
        text=True,
    )

    assert result.returncode != 0
    assert "not a git repository" in result.stderr.casefold()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["rev-parse", "--show-toplevel"], "repo-root"),
        (["rev-parse", "--is-inside-work-tree"], "true"),
        (["show", "HEAD:README.md"], "demo"),
    ],
)
def test_hardened_git_safe_exact_queries_work_with_dangerous_filter_config(
    tmp_path,
    args,
    expected,
):
    git = _commit_readme(tmp_path)
    (tmp_path / ".gitattributes").write_text(
        "README.md filter=evil\n",
        encoding="utf-8",
    )
    subprocess.run([git, "add", ".gitattributes"], cwd=tmp_path, check=True)
    subprocess.run(
        [git, "commit", "-q", "-m", "add filter attributes"],
        cwd=tmp_path,
        check=True,
    )
    marker = tmp_path / "filter-ran"
    subprocess.run(
        [git, "config", "filter.evil.clean", f"touch {marker}"],
        cwd=tmp_path,
        check=True,
    )

    result = run_hardened_git(
        git,
        args,
        cwd=tmp_path,
        check=True,
        text=True,
    )

    output = result.stdout.strip()
    if expected == "repo-root":
        assert output == str(tmp_path.resolve())
    else:
        assert output == expected
    assert not marker.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "core.fsmonitor=/tmp/marker", "status"],
        ["-ccore.fsmonitor=/tmp/marker", "status"],
        ["--config-env=core.fsmonitor=PICO_MARKER", "status"],
        ["--paginate", "status"],
        ["--exec-path=.", "status"],
        ["--git-dir=../outside", "status"],
        ["--work-tree=../outside", "status"],
        ["diff", "--ext-diff"],
        ["diff", "--textconv"],
        ["submodule", "update"],
    ],
)
def test_hardened_git_rejects_arguments_that_can_reenable_execution(
    tmp_path,
    monkeypatch,
    args,
):
    called = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: called.append((a, k)),
    )

    with pytest.raises(ValueError, match="unsafe git arguments"):
        run_hardened_git("/usr/bin/git", args, cwd=tmp_path)

    assert called == []


def test_hardened_git_disables_textconv_for_diff_rendering_commands(
    tmp_path,
    monkeypatch,
):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hardened_git(
        "/usr/bin/git",
        ["show", "HEAD:README.md"],
        cwd=tmp_path,
        text=True,
    )

    assert captured[0][-4:] == [
        "show",
        "--no-ext-diff",
        "--no-textconv",
        "HEAD:README.md",
    ]


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
    @contextmanager
    def passthrough(executable):
        yield str(executable)

    monkeypatch.setattr("pico.safe_subprocess._prepared_executable", passthrough)
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hardened_rg("/usr/bin/rg", ["needle", "."], cwd=tmp_path)

    assert captured["argv"] == ["/usr/bin/rg", "needle", "."]
    assert captured["kwargs"]["env"]["RIPGREP_CONFIG_PATH"] == os.devnull
    assert "UNRELATED_SECRET" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_hardened_rg_child_path_comes_only_from_frozen_executable(
    tmp_path,
    monkeypatch,
):
    from pico import safe_subprocess

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("PATH", str(tmp_path / "poisoned"))
    monkeypatch.setattr(
        safe_subprocess,
        "_safe_path_dirs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("live PATH inspected")
        ),
    )
    @contextmanager
    def passthrough(executable):
        yield str(executable)

    monkeypatch.setattr(safe_subprocess, "_prepared_executable", passthrough)
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hardened_rg(
        "/opt/pico-frozen/bin/rg",
        ["needle", "."],
        cwd=tmp_path,
    )

    assert captured["argv"][0] == "/opt/pico-frozen/bin/rg"
    assert captured["env"]["PATH"] == "/opt/pico-frozen/bin"
    assert captured["env"]["RIPGREP_CONFIG_PATH"] == os.devnull


@pytest.mark.parametrize("option", ["--pre", "--pre=cat", "--pre-glob", "--pre-glob=*.py"])
def test_hardened_rg_rejects_preprocessors(option, tmp_path, monkeypatch):
    def runner(*args, **kwargs):
        raise AssertionError("rg executed")

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(ValueError):
        run_hardened_rg("/usr/bin/rg", [option, "needle", "."], cwd=tmp_path)
