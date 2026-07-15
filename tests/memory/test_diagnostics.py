import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import pico.memory.block_store as block_store_module
from pico.cli import main
from pico.cli_diagnostics import collect_doctor
from pico.memory.diagnostics import collect_memory_diagnostics


def _write_note(root, name, content):
    path = root / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _init_git(root):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    return git


def test_memory_diagnostics_reports_metadata_caps_and_git_ignore(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    workspace_memory = repo / ".pico" / "memory"
    user_memory = tmp_path / "home-private-canary" / ".pico" / "memory"
    repo.mkdir()
    git = _init_git(repo)
    (repo / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    _write_note(
        workspace_memory,
        "a.md",
        "---\nname: shared\nsupersedes: [gone]\n---\nworkspace body\n",
    )
    _write_note(
        user_memory,
        "b.md",
        "---\nname: shared\n---\nuser body\n",
    )
    _write_note(
        workspace_memory,
        "invalid.md",
        "---\nname without a colon\n---\nprivate body\n",
    )
    (workspace_memory / "agent_notes.md").write_text("workspace note", encoding="utf-8")
    (user_memory / "agent_notes.md").write_text("user note", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "AGENT_NOTES_SOFT_LIMIT_CHARS", 4)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=user_memory,
        git_executable=git,
    )

    assert result["status"] == "warn"
    assert all(
        set(issue) == {"path", "count", "reason_code", "limit"}
        for issue in result["issues"]
    )
    issues = {
        (item["path"], item["reason_code"]): (item["count"], item["limit"])
        for item in result["issues"]
    }
    assert issues[("workspace/notes/a.md", "duplicate_frontmatter_name")] == (2, 1)
    assert issues[("user/notes/b.md", "duplicate_frontmatter_name")] == (2, 1)
    assert issues[("workspace/notes/a.md", "missing_supersedes_target")] == (1, 0)
    assert issues[("workspace/notes/invalid.md", "invalid_frontmatter")] == (1, 0)
    assert issues[("workspace/agent_notes.md", "agent_notes_soft_limit_exceeded")] == (
        len("workspace note"),
        4,
    )
    assert issues[("user/agent_notes.md", "agent_notes_soft_limit_exceeded")] == (
        len("user note"),
        4,
    )
    assert issues[("workspace/notes/a.md", "workspace_user_note_git_ignored")] == (1, 0)
    assert issues[("workspace/notes/invalid.md", "workspace_user_note_git_ignored")] == (1, 0)


def test_memory_diagnostics_do_not_leak_content_frontmatter_or_user_root(tmp_path):
    canary = "memory-diagnostic-secret-canary"
    repo = tmp_path / "repo"
    user_memory = tmp_path / canary / ".pico" / "memory"
    repo.mkdir()
    _write_note(
        repo / ".pico" / "memory",
        "one.md",
        f"---\nname: {canary}\nsupersedes: [{canary}-missing]\n---\n{canary}-body\n",
    )
    _write_note(
        user_memory,
        "two.md",
        f"---\nname: {canary}\n---\n{canary}-user-body\n",
    )

    result = collect_memory_diagnostics(repo, user_memory_root=user_memory)
    serialized = json.dumps(result)

    assert canary not in serialized
    assert str(user_memory) not in serialized
    assert {issue["path"] for issue in result["issues"]} == {
        "user/notes/two.md",
        "workspace/notes/one.md",
    }


@pytest.mark.parametrize(
    ("limit_name", "limit", "files", "reason_code", "expected_path", "count"),
    (
        (
            "MAX_MEMORY_INDEX_FILES",
            1,
            {"a.md": b"a", "b.md": b"b"},
            "memory_file_count_limit_reached",
            "workspace/notes/b.md",
            2,
        ),
        (
            "MAX_MEMORY_FILE_BYTES",
            8,
            {"a.md": b"x" * 9},
            "memory_file_size_limit_reached",
            "workspace/notes/a.md",
            9,
        ),
        (
            "MAX_MEMORY_INDEX_BYTES",
            8,
            {"a.md": b"a" * 5, "b.md": b"b" * 5},
            "memory_total_bytes_limit_reached",
            "workspace/notes/b.md",
            9,
        ),
    ),
)
def test_memory_diagnostics_report_scan_limits(
    tmp_path,
    monkeypatch,
    limit_name,
    limit,
    files,
    reason_code,
    expected_path,
    count,
):
    repo = tmp_path / "repo"
    notes = repo / ".pico" / "memory" / "notes"
    notes.mkdir(parents=True)
    for name, content in files.items():
        (notes / name).write_bytes(content)
    monkeypatch.setattr(block_store_module, limit_name, limit)

    result = collect_memory_diagnostics(repo, user_memory_root=tmp_path / "missing-user")

    issue = next(item for item in result["issues"] if item["reason_code"] == reason_code)
    assert issue == {
        "path": expected_path,
        "count": count,
        "reason_code": reason_code,
        "limit": limit,
    }


@pytest.mark.parametrize("entry_kind", ("directory", "non_markdown"))
def test_memory_diagnostics_bound_non_candidate_traversal(
    tmp_path,
    monkeypatch,
    entry_kind,
):
    repo = tmp_path / "repo"
    notes = repo / ".pico" / "memory" / "notes"
    notes.mkdir(parents=True)
    for index in range(3):
        path = notes / f"entry-{index}"
        if entry_kind == "directory":
            path.mkdir()
        else:
            path.with_suffix(".txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_FILES", 2)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
    )

    assert next(
        item
        for item in result["issues"]
        if item["reason_code"] == "memory_scan_entry_limit_reached"
    ) == {
        "path": "workspace/notes",
        "count": 3,
        "reason_code": "memory_scan_entry_limit_reached",
        "limit": 2,
    }


@pytest.mark.parametrize(
    "frontmatter",
    (
        "tags: not-a-list",
        "name:",
        "unknown: value",
        "",
    ),
)
def test_memory_diagnostics_report_invalid_recognized_frontmatter(
    tmp_path,
    frontmatter,
):
    repo = tmp_path / "repo"
    _write_note(
        repo / ".pico" / "memory",
        "invalid.md",
        f"---\n{frontmatter}\n---\nbody\n",
    )

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
    )

    assert any(
        item["path"] == "workspace/notes/invalid.md"
        and item["reason_code"] == "invalid_frontmatter"
        for item in result["issues"]
    )


def test_memory_diagnostics_fail_closed_on_read_and_scan_errors(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_note(repo / ".pico" / "memory", "one.md", "body\n")

    def unreadable(*args, **kwargs):
        del args, kwargs
        raise PermissionError("private-read-canary")

    monkeypatch.setattr("pico.memory.diagnostics._read_bounded_regular", unreadable)
    read_result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
    )

    assert read_result["status"] == "unknown"
    assert read_result["reason_code"] == "memory_diagnostics_incomplete"
    assert any(
        item["reason_code"] == "memory_file_read_failed"
        for item in read_result["issues"]
    )
    assert "private-read-canary" not in json.dumps(read_result)

    def scan_failed(*args, **kwargs):
        del args, kwargs
        raise PermissionError("private-scan-canary")

    monkeypatch.setattr("pico.memory.diagnostics.os.scandir", scan_failed)
    scan_result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
    )

    assert scan_result["status"] == "unknown"
    assert any(
        item["reason_code"] == "memory_scan_failed"
        for item in scan_result["issues"]
    )
    assert "private-scan-canary" not in json.dumps(scan_result)


def test_memory_diagnostics_do_not_follow_notes_symlink(tmp_path):
    repo = tmp_path / "repo"
    memory = repo / ".pico" / "memory"
    outside = tmp_path / "private-symlink-canary"
    outside.mkdir()
    (outside / "secret.md").write_text("private-body-canary", encoding="utf-8")
    memory.mkdir(parents=True)
    (memory / "notes").symlink_to(outside, target_is_directory=True)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
    )

    assert result["status"] == "unknown"
    assert any(
        item["path"] == "workspace/notes"
        and item["reason_code"] == "memory_scan_failed"
        for item in result["issues"]
    )
    assert "private-symlink-canary" not in json.dumps(result)
    assert "private-body-canary" not in json.dumps(result)


def test_memory_diagnostics_git_failure_is_unknown_and_low_sensitivity(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)

    monkeypatch.setattr(
        "pico.memory.diagnostics.run_hardened_git",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout=b"",
            stderr=b"private-git-canary",
        ),
    )

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
        git_executable="/trusted/git",
    )

    assert result["status"] == "unknown"
    assert any(
        item == {
            "path": "workspace/notes/__pico_git_ignore_probe__.md",
            "count": 1,
            "reason_code": "memory_git_ignore_check_failed",
            "limit": 0,
        }
        for item in result["issues"]
    )
    assert "private-git-canary" not in json.dumps(result)


def test_doctor_contains_memory_git_timeout(tmp_path, monkeypatch):
    _init_git(tmp_path)
    monkeypatch.setattr(
        "pico.memory.diagnostics.Path.home",
        lambda: tmp_path / "missing-home",
    )

    def timeout(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(["git", "check-ignore"], 5)

    monkeypatch.setattr("pico.memory.diagnostics.run_hardened_git", timeout)

    result = collect_doctor(tmp_path, offline=True)

    assert result["memory"]["status"] == "unknown"
    assert result["memory"]["reason_code"] == "memory_diagnostics_incomplete"
    assert any(
        item["reason_code"] == "memory_git_ignore_check_failed"
        for item in result["memory"]["issues"]
    )


def test_memory_diagnostics_check_git_ignore_with_empty_notes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _init_git(repo)
    (repo / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    (repo / ".pico" / "memory" / "notes").mkdir(parents=True)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
        git_executable=git,
    )

    assert {
        "path": "workspace/notes/__pico_git_ignore_probe__.md",
        "count": 1,
        "reason_code": "workspace_user_note_git_ignored",
        "limit": 0,
    } in result["issues"]


def test_memory_diagnostics_use_doctor_check_contract(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _init_git(repo)

    healthy = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
        git_executable=git,
    )
    assert healthy == {
        "check_id": "memory",
        "status": "pass",
        "reason_code": "memory_diagnostics_passed",
        "remediation": "",
        "issues": [],
    }

    _write_note(
        repo / ".pico" / "memory",
        "invalid.md",
        "---\ninvalid line\n---\nbody\n",
    )
    warning = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user",
        git_executable=git,
    )
    assert warning["check_id"] == "memory"
    assert warning["status"] == "warn"
    assert warning["reason_code"] == "memory_review_required"
    assert warning["remediation"] == "review Memory note metadata and Git ignore rules"
    assert warning["status"] in {
        "pass",
        "warn",
        "fail",
        "not_applicable",
        "unknown",
    }


def test_doctor_memory_diagnostics_are_read_only_and_render_in_both_formats(
    tmp_path,
    monkeypatch,
    capsys,
):
    user_memory = tmp_path / "user-home" / ".pico" / "memory"
    workspace_memory = tmp_path / ".pico" / "memory"
    _write_note(
        workspace_memory,
        "invalid.md",
        "---\ninvalid line\n---\nbody-canary\n",
    )
    (workspace_memory / "agent_notes.md").write_text("agent", encoding="utf-8")
    (workspace_memory / "agent_notes.md").chmod(0o644)
    before_mode = (workspace_memory / "agent_notes.md").stat().st_mode
    monkeypatch.setattr("pico.memory.diagnostics.Path.home", lambda: tmp_path / "user-home")

    data = collect_doctor(tmp_path, offline=True)

    assert data["memory"]["issues"] == [
        {
            "path": "workspace/notes/invalid.md",
            "count": 1,
            "reason_code": "invalid_frontmatter",
            "limit": 0,
        }
    ]
    assert (workspace_memory / "agent_notes.md").stat().st_mode == before_mode
    assert not user_memory.exists()

    for output_format in ("json", "text"):
        assert main([
            "--cwd",
            str(tmp_path),
            "--format",
            output_format,
            "doctor",
            "--offline",
        ]) == 0
        output = capsys.readouterr().out
        assert "Memory" in output or '"memory"' in output
        assert "invalid_frontmatter" in output
        assert "body-canary" not in output
        if output_format == "json":
            rendered_memory = json.loads(output)["data"]["memory"]
            assert set(rendered_memory) == {
                "check_id",
                "status",
                "reason_code",
                "remediation",
                "issues",
            }
            assert rendered_memory["status"] == "warn"
    assert not user_memory.exists()


def test_doctor_does_not_create_missing_memory_directories(tmp_path, monkeypatch):
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("pico.memory.diagnostics.Path.home", lambda: user_home)

    result = collect_doctor(tmp_path, offline=True)

    assert result["memory"] == {
        "check_id": "memory",
        "status": "pass",
        "reason_code": "memory_diagnostics_passed",
        "remediation": "",
        "issues": [],
    }
    assert not (tmp_path / ".pico" / "memory").exists()
    assert not (user_home / ".pico" / "memory").exists()
