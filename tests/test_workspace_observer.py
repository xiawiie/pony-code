import subprocess

from pico.workspace_observer import WorkspaceObserver


def test_workspace_observer_detects_delta_without_git(tmp_path):
    observer = WorkspaceObserver(tmp_path)
    before = observer.capture()
    (tmp_path / "generated.txt").write_text("hello", encoding="utf-8")
    after = observer.capture()

    delta = observer.diff(before, after)

    assert delta["changed_paths"] == ["generated.txt"]
    assert delta["summaries"] == ["created:generated.txt"]


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
