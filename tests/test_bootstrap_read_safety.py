import json
import os
import subprocess
from pathlib import Path

import pytest

from pico.workspace import context as workspace_module
from pico.memory.block_store import BlockStore
from pico.memory.repo_map import RepoMap
from pico.tools.subprocess import build_trusted_executables
from pico.workspace.context import WorkspaceContext
from pico.workspace.observer import WorkspaceObserver


def _trusted_binary(workspace, name):
    executable = build_trusted_executables(workspace, names=(name,)).get(name)
    if not executable:
        pytest.skip(f"trusted {name} is unavailable")
    return executable


def test_workspace_context_does_not_follow_readme_symlink_to_secret(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    (tmp_path / ".env").write_text(f"PICO_TOKEN={secret}\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(tmp_path / ".env")

    workspace = WorkspaceContext.build(tmp_path)

    assert secret not in workspace.stable_text()
    assert "README.md" not in workspace.project_docs


@pytest.mark.parametrize("name", ("AGENTS.md", "pyproject.toml", "package.json"))
def test_workspace_context_does_not_follow_project_doc_symlink(tmp_path, name):
    outside = tmp_path.parent / f"{tmp_path.name}-{name.replace('.', '-')}-outside"
    outside.write_text("outside-secret-123456789", encoding="utf-8")
    (tmp_path / name).symlink_to(outside)

    workspace = WorkspaceContext.build(tmp_path)

    assert "outside-secret" not in workspace.stable_text()
    assert name not in workspace.project_docs


def test_bootstrap_reader_rejects_symlinked_parent_and_sensitive_file(tmp_path):
    from pico.workspace.context import _safe_index_file

    outside = tmp_path.parent / f"{tmp_path.name}-outside-docs"
    outside.mkdir()
    (outside / "README.md").write_text("parent-link-secret", encoding="utf-8")
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    (tmp_path / ".env").write_text("PICO_TOKEN=opaque", encoding="utf-8")

    assert _safe_index_file(tmp_path, tmp_path / "docs" / "README.md") is None
    assert _safe_index_file(tmp_path, tmp_path / ".env") is None


def test_global_agents_rejects_symlink_and_redacts_before_clip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    global_dir = home / ".pico"
    repo = tmp_path / "repo"
    global_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    outside = tmp_path / "outside-agents.md"
    outside.write_text("outside-global-secret-123456789", encoding="utf-8")
    global_agents = global_dir / "AGENTS.md"
    global_agents.symlink_to(outside)

    workspace = WorkspaceContext.build(repo)
    assert "outside-global-secret" not in workspace.stable_text()
    assert "<global>/AGENTS.md" not in workspace.project_docs

    global_agents.unlink()
    secret = "github_pat_" + "B" * 40
    global_agents.write_text("x" * 1480 + "\n" + secret, encoding="utf-8")
    workspace = WorkspaceContext.build(repo)
    rendered = workspace.project_docs["<global>/AGENTS.md"]
    assert secret not in rendered
    assert "<redacted>" in rendered


def test_global_agents_home_lookup_failure_is_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path,
        "home",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("home unavailable"))),
    )

    workspace = WorkspaceContext.build(tmp_path)

    assert "<global>/AGENTS.md" not in workspace.project_docs


def test_workspace_context_rejects_mutable_executable_during_discovery(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    safe_bin = tmp_path / "safe-bin"
    repo.mkdir()
    safe_bin.mkdir(mode=0o755)
    executable = safe_bin / "git"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    real_build = build_trusted_executables

    def discover_mutable(root):
        trusted = real_build(
            root,
            env={"PATH": str(safe_bin)},
            names=("git",),
        )
        assert trusted == {}
        return trusted

    monkeypatch.setattr(
        workspace_module,
        "build_trusted_executables",
        discover_mutable,
    )

    workspace = WorkspaceContext.build(repo)

    assert "git" not in workspace.trusted_executables
    assert workspace.branch == "-"


def test_workspace_context_skips_oversized_automatic_document(tmp_path):
    oversized = b"x" * (workspace_module.MAX_BOOTSTRAP_FILE_BYTES + 1)
    (tmp_path / "README.md").write_bytes(oversized)

    workspace = WorkspaceContext.build(tmp_path, executables={})

    assert "README.md" not in workspace.project_docs


def test_workspace_context_limits_automatic_document_count(tmp_path, monkeypatch):
    names = tuple(
        f"bootstrap-{index}.txt"
        for index in range(workspace_module.MAX_BOOTSTRAP_FILES + 1)
    )
    monkeypatch.setattr(workspace_module, "DOC_NAMES", names)
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")

    workspace = WorkspaceContext.build(tmp_path, executables={})

    assert len(workspace.project_docs) == workspace_module.MAX_BOOTSTRAP_FILES
    assert names[-1] not in workspace.project_docs


def test_workspace_context_limits_aggregate_automatic_document_bytes(
    tmp_path,
    monkeypatch,
):
    payload = b"x" * workspace_module.MAX_BOOTSTRAP_FILE_BYTES
    count = workspace_module.MAX_BOOTSTRAP_TOTAL_BYTES // len(payload) + 1
    assert count <= workspace_module.MAX_BOOTSTRAP_FILES
    names = tuple(f"aggregate-{index}.txt" for index in range(count))
    monkeypatch.setattr(workspace_module, "DOC_NAMES", names)
    for name in names:
        (tmp_path / name).write_bytes(payload)

    workspace = WorkspaceContext.build(tmp_path, executables={})

    assert names[-1] not in workspace.project_docs
    assert len(workspace.project_docs) == count - 1


def test_workspace_bounded_reader_rejects_parent_swap(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    outside = tmp_path / "outside"
    docs.mkdir(parents=True)
    outside.mkdir()
    target = docs / "README.md"
    target.write_text("inside\n", encoding="utf-8")
    (outside / "README.md").write_text("outside-canary\n", encoding="utf-8")
    validated = workspace_module._safe_index_file(repo, target)
    moved = repo / "docs-original"
    real_open_directory = workspace_module.private_files._open_private_directory

    def swap_then_open(path):
        docs.rename(moved)
        docs.symlink_to(outside, target_is_directory=True)
        return real_open_directory(path)

    monkeypatch.setattr(
        workspace_module.private_files,
        "_open_private_directory",
        swap_then_open,
    )

    with pytest.raises((OSError, ValueError)):
        workspace_module._read_bounded_regular(validated, 1024)


def test_repo_map_and_memory_index_skip_symlink_files_in_both_scopes(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-source"
    outside.write_text("def SecretSymbol():\n    pass\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)

    repo_map = RepoMap(tmp_path)
    repo_map.scan()
    assert "SecretSymbol" not in json.dumps(
        [item.__dict__ for item in repo_map.lookup("SecretSymbol")]
    )

    workspace_memory = tmp_path / ".pico" / "memory"
    user_memory = tmp_path / "user-memory"
    (workspace_memory / "notes").mkdir(parents=True)
    (user_memory / "notes").mkdir(parents=True)
    (workspace_memory / "notes" / "linked.md").symlink_to(outside)
    (user_memory / "notes" / "linked.md").symlink_to(outside)
    store = BlockStore(workspace_memory, user_memory)
    assert all("linked.md" not in entry.path for entry in store.list())


def test_workspace_and_observer_disable_repository_fsmonitor(tmp_path):
    trusted_git = _trusted_binary(tmp_path, "git")
    marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor.sh"
    fsmonitor.write_text(
        f"#!/bin/sh\ntouch {marker!s}\nexit 0\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o755)
    subprocess.run([trusted_git, "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [trusted_git, "config", "core.fsmonitor", str(fsmonitor)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    marker.unlink(missing_ok=True)
    executables = {"git": trusted_git}

    workspace = WorkspaceContext.build(tmp_path, executables=executables)
    assert not marker.exists()
    assert workspace.trusted_executables == executables

    observer = WorkspaceObserver(tmp_path, executables=executables)
    observer.capture()
    assert not marker.exists()


def test_workspace_observer_without_frozen_git_never_runs_subprocess(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bare git executed")
        ),
    )

    snapshot = WorkspaceObserver(tmp_path, executables={}).capture()

    assert snapshot["mode"] == "filesystem"


def test_search_ignores_inherited_ripgrep_preprocessor(
    tmp_path, monkeypatch, contract_rg
):
    from pico.tools.context import ToolContext
    from pico.tools.search import tool_search

    marker = tmp_path / "rg-pre-ran"
    pre = tmp_path / "pre.sh"
    pre.write_text(
        f'#!/bin/sh\ntouch {marker!s}\ncat "$1"\n',
        encoding="utf-8",
    )
    pre.chmod(0o755)
    config = tmp_path / "ripgrep.conf"
    config.write_text(
        f"--pre={pre}\n--pre-glob=*.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "normal.txt").write_text("expected needle\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw: Path(os.path.abspath(tmp_path / raw)),
        shell_env_provider=dict,
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={"rg": contract_rg},
    )

    result = tool_search(context, {"pattern": "needle", "path": "."})

    assert "normal.txt:1:expected needle" in result
    assert not marker.exists()
