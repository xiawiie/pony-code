import subprocess

import pytest

from pony.workspace.observer import WorkspaceObserver


def test_workspace_observer_detects_delta_without_git(tmp_path):
    observer = WorkspaceObserver(tmp_path)
    before = observer.capture()
    (tmp_path / "generated.txt").write_text("hello", encoding="utf-8")
    after = observer.capture()

    delta = observer.diff(before, after)

    assert delta["changed_paths"] == ["generated.txt"]
    assert delta["summaries"] == ["created:generated.txt"]


def test_workspace_observer_detects_same_size_large_file_replacement(tmp_path):
    path = tmp_path / "large.bin"
    path.write_bytes(b"a" * (8 * 1024 * 1024 + 1))
    observer = WorkspaceObserver(tmp_path, executables={})
    before = observer.capture()
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"b" * path.stat().st_size)
    replacement.replace(path)

    delta = observer.diff(before, observer.capture())

    assert delta["changed_paths"] == ["large.bin"]
    assert delta["summaries"] == ["modified:large.bin"]


def test_workspace_observer_detects_git_dirty_delta(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True, env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin"})

    observer = WorkspaceObserver(tmp_path)
    before = observer.capture()
    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    after = observer.capture()

    delta = observer.diff(before, after)

    assert delta["changed_paths"] == ["tracked.txt"]


def test_workspace_observer_uses_frozen_hardened_git(tmp_path, monkeypatch):
    calls = []

    def fake_git(executable, args, **kwargs):
        calls.append((executable, list(args), kwargs))
        stdout = "true\n" if args == ["rev-parse", "--is-inside-work-tree"] else b""
        return subprocess.CompletedProcess([executable, *args], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        "pony.workspace.observer.run_hardened_git",
        fake_git,
        raising=False,
    )
    observer = WorkspaceObserver(tmp_path, executables={"git": "/frozen/git"})

    snapshot = observer.capture()

    assert snapshot["mode"] == "git"
    assert [call[1] for call in calls] == [
        ["rev-parse", "--is-inside-work-tree"],
        ["status", "--porcelain=v1", "-z", "-uall"],
    ]
    assert all(call[0] == "/frozen/git" for call in calls)


def test_filesystem_observer_skips_symlink_and_sensitive_paths(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    (tmp_path / ".env").write_text("PONY_TOKEN=opaque", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")

    snapshot = WorkspaceObserver(tmp_path, executables={}).capture()

    assert "linked.txt" not in snapshot["paths"]
    assert ".env" not in snapshot["paths"]
    assert snapshot["paths"].keys() == {"safe.txt"}


def test_workspace_observer_rejects_symlinked_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-not-scan.txt").write_text("outside", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)

    snapshot = WorkspaceObserver(linked_root, executables={}).capture()

    assert snapshot["paths"] == {}


def test_git_observer_preserves_tracked_deletion_marker(tmp_path, monkeypatch):
    calls = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=b" D deleted.txt\x00", stderr=b""),
        ]
    )
    monkeypatch.setattr(
        "pony.workspace.observer.run_hardened_git",
        lambda *args, **kwargs: next(calls),
        raising=False,
    )

    snapshot = WorkspaceObserver(tmp_path, executables={"git": "/frozen/git"}).capture()

    assert snapshot["paths"] == {"deleted.txt": "D"}
    assert snapshot["detail"] == {}


@pytest.mark.parametrize("style", ("keyword", "positional"))
def test_legacy_bare_git_value_is_accepted_but_never_executed(
    style,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "pony.workspace.observer.run_hardened_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bare git executed")
        ),
    )

    if style == "keyword":
        observer = WorkspaceObserver(tmp_path, git_binary="git")
    else:
        observer = WorkspaceObserver(tmp_path, "git")

    assert observer.capture()["mode"] == "filesystem"
    assert dict(observer.trusted_executables) == {}


@pytest.mark.parametrize("style", ("keyword", "positional"))
def test_validated_legacy_absolute_git_uses_hardened_runner(
    style,
    tmp_path,
    monkeypatch,
):
    executable = tmp_path.parent / f"{tmp_path.name}-trusted-git"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = []

    def fake_git(git, args, **kwargs):
        calls.append((git, list(args)))
        stdout = "true\n" if args == ["rev-parse", "--is-inside-work-tree"] else b""
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr("pony.workspace.observer.run_hardened_git", fake_git)
    if style == "keyword":
        observer = WorkspaceObserver(tmp_path, git_binary=str(executable))
    else:
        observer = WorkspaceObserver(tmp_path, str(executable))

    assert observer.capture()["mode"] == "git"
    assert observer.trusted_executables["git"] == str(executable.resolve())
    assert calls == [
        (str(executable.resolve()), ["rev-parse", "--is-inside-work-tree"]),
        (str(executable.resolve()), ["status", "--porcelain=v1", "-z", "-uall"]),
    ]


@pytest.mark.parametrize("unsafe_kind", ("inside_workspace", "writable", "not_executable", "missing"))
def test_unsafe_legacy_absolute_git_is_ignored(
    unsafe_kind,
    tmp_path,
    monkeypatch,
):
    if unsafe_kind == "inside_workspace":
        executable = tmp_path / "git"
        mode = 0o755
    else:
        executable = tmp_path.parent / f"{tmp_path.name}-{unsafe_kind}-git"
        mode = 0o777 if unsafe_kind == "writable" else 0o644
    if unsafe_kind != "missing":
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(mode)
    monkeypatch.setattr(
        "pony.workspace.observer.run_hardened_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe git executed")
        ),
    )

    observer = WorkspaceObserver(tmp_path, git_binary=str(executable))

    assert observer.capture()["mode"] == "filesystem"
    assert dict(observer.trusted_executables) == {}


def test_second_positional_executable_mapping_remains_supported(tmp_path, monkeypatch):
    calls = []

    def fake_git(executable, args, **kwargs):
        calls.append(executable)
        stdout = "true\n" if args == ["rev-parse", "--is-inside-work-tree"] else b""
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr("pony.workspace.observer.run_hardened_git", fake_git)

    snapshot = WorkspaceObserver(tmp_path, {"git": "/frozen/git"}).capture()

    assert snapshot["mode"] == "git"
    assert calls == ["/frozen/git", "/frozen/git"]
