import os
from pathlib import Path
import stat
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico import security as securitylib
from pico import tools as tool_module
from pico.providers.fake import FakeModelClient
from pico.tool_context import ToolContext


def _context(root):
    return ToolContext(
        root=root,
        path_resolver=lambda raw_path: (root / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(root)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda _args: "unused",
        trusted_executables={},
        workspace_root_identity=securitylib.private_directory_identity(root),
    )


def _agent(root):
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy="auto",
    )


def _make_unsafe_entry(root, outside, kind):
    target = root / "unsafe"
    outside_file = outside / "outside.txt"
    outside_file.write_text("outside-canary\n", encoding="utf-8")
    if kind == "symlink":
        target.symlink_to(outside_file)
    elif kind == "hardlink":
        os.link(outside_file, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    elif kind == "directory":
        target.mkdir()
    else:
        raise AssertionError(kind)
    return target, outside_file


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "directory"))
def test_anchored_reader_rejects_unsafe_leaf_without_exposing_outside(
    tmp_path,
    kind,
):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _target, outside_file = _make_unsafe_entry(root, outside, kind)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.read_regular_bytes_anchored(
            root,
            "unsafe",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity(root),
        )

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert outside_file.read_text(encoding="utf-8") == "outside-canary\n"


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="no /dev/null")
def test_anchored_reader_rejects_device_before_read(monkeypatch):
    reads = Mock(side_effect=AssertionError("device content was read"))
    monkeypatch.setattr(securitylib.os, "read", reads)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.read_regular_bytes_anchored(
            "/dev",
            "null",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity("/dev"),
        )

    assert exc_info.value.code == "workspace_entry_unsafe"
    reads.assert_not_called()


def test_reader_revalidates_parent_before_reading_after_final_open_swap(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    parent = root / "parent"
    outside = tmp_path / "outside"
    detached = tmp_path / "detached"
    parent.mkdir(parents=True)
    outside.mkdir()
    (parent / "note.txt").write_text("inside\n", encoding="utf-8")
    outside_target = outside / "note.txt"
    outside_target.write_text("outside-canary\n", encoding="utf-8")
    outside_identity = (
        outside_target.stat().st_dev,
        outside_target.stat().st_ino,
    )
    real_open = securitylib.os.open
    real_read = securitylib.os.read
    swapped = False
    outside_reads = 0

    def swap_parent(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "note.txt" and kwargs.get("dir_fd") is not None and not swapped:
            parent.rename(detached)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    def track_read(descriptor, size):
        nonlocal outside_reads
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == outside_identity:
            outside_reads += 1
        return real_read(descriptor, size)

    monkeypatch.setattr(securitylib.os, "open", swap_parent)
    monkeypatch.setattr(securitylib.os, "read", track_read)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.read_regular_bytes_anchored(
            root,
            "parent/note.txt",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity(root),
        )

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert swapped is True
    assert outside_reads == 0
    assert outside_target.read_text(encoding="utf-8") == "outside-canary\n"


def test_reader_rejects_target_inode_exchange_before_any_content_read(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "note.txt"
    target.write_text("inside\n", encoding="utf-8")
    outside_target = outside / "outside.txt"
    outside_target.write_text("outside-canary\n", encoding="utf-8")
    outside_identity = (
        outside_target.stat().st_dev,
        outside_target.stat().st_ino,
    )
    real_open = securitylib.os.open
    real_read = securitylib.os.read
    swapped = False
    outside_reads = 0

    def swap_target(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "note.txt" and kwargs.get("dir_fd") is not None and not swapped:
            target.unlink()
            os.link(outside_target, target)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    def track_read(descriptor, size):
        nonlocal outside_reads
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == outside_identity:
            outside_reads += 1
        return real_read(descriptor, size)

    monkeypatch.setattr(securitylib.os, "open", swap_target)
    monkeypatch.setattr(securitylib.os, "read", track_read)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.read_regular_bytes_anchored(
            root,
            "note.txt",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity(root),
        )

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert swapped is True
    assert outside_reads == 0


def test_reader_rejects_workspace_root_replacement_before_leaf_open(tmp_path):
    root = tmp_path / "root"
    detached = tmp_path / "detached"
    root.mkdir()
    (root / "note.txt").write_text("inside\n", encoding="utf-8")
    identity = securitylib.private_directory_identity(root)
    root.rename(detached)
    root.mkdir()
    (root / "note.txt").write_text("replacement-canary\n", encoding="utf-8")

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.read_regular_bytes_anchored(
            root,
            "note.txt",
            max_bytes=1024,
            expected_root_identity=identity,
        )

    assert exc_info.value.code == "workspace_entry_unsafe"


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "directory"))
def test_atomic_writer_rejects_unsafe_target_without_outside_write(
    tmp_path,
    kind,
):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _target, outside_file = _make_unsafe_entry(root, outside, kind)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.write_regular_bytes_anchored_atomic(
            root,
            "unsafe",
            b"replacement\n",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity(root),
        )

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert outside_file.read_text(encoding="utf-8") == "outside-canary\n"


def test_atomic_writer_revalidates_parent_before_writing_temp(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    parent = root / "parent"
    outside = tmp_path / "outside"
    detached = tmp_path / "detached"
    parent.mkdir(parents=True)
    outside.mkdir()
    outside_target = outside / "note.txt"
    outside_target.write_text("outside-canary\n", encoding="utf-8")
    real_open = securitylib.os.open
    swapped = False

    def swap_parent(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            isinstance(path, str)
            and path.startswith(".note.txt.")
            and path.endswith(".tmp")
            and not swapped
        ):
            parent.rename(detached)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(securitylib.os, "open", swap_parent)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.write_regular_bytes_anchored_atomic(
            root,
            "parent/note.txt",
            b"pico-write\n",
            max_bytes=1024,
            expected_root_identity=securitylib.private_directory_identity(root),
        )

    assert exc_info.value.code == "workspace_changed_during_write"
    assert swapped is True
    assert outside_target.read_text(encoding="utf-8") == "outside-canary\n"
    assert not list(detached.glob(".note.txt.*.tmp"))


def test_patch_uses_read_digest_as_atomic_write_cas(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    context = _context(tmp_path)
    original_write = securitylib.write_regular_bytes_anchored_atomic

    def external_change(*args, **kwargs):
        target.write_text("external-change\n", encoding="utf-8")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        securitylib,
        "write_regular_bytes_anchored_atomic",
        external_change,
    )

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        tool_module.tool_patch_file(
            context,
            {"path": "note.txt", "old_text": "before", "new_text": "after"},
        )

    assert exc_info.value.code == "workspace_changed_during_write"
    assert target.read_text(encoding="utf-8") == "external-change\n"


def test_atomic_writer_preserves_existing_mode_and_uses_0644_for_new_file(
    tmp_path,
):
    existing = tmp_path / "existing.txt"
    existing.write_text("old\n", encoding="utf-8")
    existing.chmod(0o640)
    identity = securitylib.private_directory_identity(tmp_path)

    securitylib.write_regular_bytes_anchored_atomic(
        tmp_path,
        "existing.txt",
        b"new\n",
        max_bytes=1024,
        expected_root_identity=identity,
    )
    securitylib.write_regular_bytes_anchored_atomic(
        tmp_path,
        "nested/new.txt",
        b"new\n",
        max_bytes=1024,
        expected_root_identity=identity,
    )

    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "nested" / "new.txt").stat().st_mode) == 0o644
    assert not list(tmp_path.rglob("*.tmp"))


def test_directory_listing_bounds_results_and_counts_unsafe_entries(tmp_path):
    for index in range(205):
        (tmp_path / f"safe-{index:03}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "unsafe-link").symlink_to(tmp_path / "safe-000.txt")
    context = _context(tmp_path)

    result = tool_module.tool_list_files(context, {"path": "."})

    result_lines = [line for line in result.splitlines() if line.startswith("[F]")]
    assert len(result_lines) == 200
    assert "[unsafe skipped: 1]" in result


def test_directory_scan_limit_has_stable_reason(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        securitylib.list_directory_names_anchored(
            tmp_path,
            ".",
            max_entries=2,
            expected_root_identity=securitylib.private_directory_identity(tmp_path),
        )

    assert exc_info.value.code == "workspace_directory_limit_exceeded"


def test_python_search_limit_has_stable_reason(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")
    context = _context(tmp_path)
    monkeypatch.setattr(tool_module, "MAX_WORKSPACE_SEARCH_FILES", 2)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        tool_module.tool_search(context, {"pattern": "needle", "path": "."})

    assert exc_info.value.code == "workspace_search_limit_exceeded"


def test_python_search_depth_limit_has_stable_reason(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("needle\n", encoding="utf-8")
    context = _context(tmp_path)
    monkeypatch.setattr(tool_module, "MAX_WORKSPACE_SEARCH_DEPTH", 0)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        tool_module.tool_search(context, {"pattern": "needle", "path": "."})

    assert exc_info.value.code == "workspace_search_limit_exceeded"


def test_python_search_total_byte_limit_has_stable_reason(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("needle\n", encoding="utf-8")
    context = _context(tmp_path)
    monkeypatch.setattr(tool_module, "MAX_WORKSPACE_SEARCH_BYTES", 3)

    with pytest.raises(securitylib.WorkspaceIOError) as exc_info:
        tool_module.tool_search(context, {"pattern": "needle", "path": "."})

    assert exc_info.value.code == "workspace_search_limit_exceeded"


def test_rg_path_falls_back_to_anchored_search_when_tree_has_hardlink(
    tmp_path,
    monkeypatch,
):
    outside = tmp_path.parent / f"{tmp_path.name}-search-outside"
    outside.write_text("outside-canary needle\n", encoding="utf-8")
    os.link(outside, tmp_path / "outside-link.txt")
    (tmp_path / "safe.txt").write_text("safe needle\n", encoding="utf-8")
    context = _context(tmp_path)
    context.trusted_executables = {"rg": "/frozen/rg"}
    rg = Mock(side_effect=AssertionError("rg received an unsafe tree"))
    monkeypatch.setattr(tool_module, "run_hardened_rg", rg)

    result = tool_module.tool_search(
        context,
        {"pattern": "needle", "path": "."},
    )

    assert result == "safe.txt:1:safe needle"
    rg.assert_not_called()
    assert outside.read_text(encoding="utf-8") == "outside-canary needle\n"


@pytest.mark.parametrize(
    ("setup", "tool_name", "arguments", "expected_code"),
    [
        (
            "hardlink",
            "read_file",
            {"path": "unsafe.txt"},
            "workspace_entry_unsafe",
        ),
        (
            "oversized",
            "read_file",
            {"path": "unsafe.txt"},
            "workspace_file_limit_exceeded",
        ),
        (
            "hardlink",
            "write_file",
            {"path": "unsafe.txt", "content": "replacement"},
            "workspace_entry_unsafe",
        ),
    ],
)
def test_tool_executor_surfaces_stable_workspace_reason_codes(
    tmp_path,
    setup,
    tool_name,
    arguments,
    expected_code,
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-{tool_name}"
    outside.write_text("outside-canary\n", encoding="utf-8")
    target = tmp_path / "unsafe.txt"
    if setup == "hardlink":
        os.link(outside, target)
    else:
        target.write_bytes(b"x" * (tool_module.MAX_WORKSPACE_FILE_BYTES + 1))
    agent = _agent(tmp_path)
    runner = Mock(return_value="must not run")
    agent.tools[tool_name]["run"] = runner

    result = agent.execute_tool(tool_name, arguments)

    assert result.metadata["tool_error_code"] == expected_code
    assert result.metadata["security_event_type"] == expected_code
    if setup == "hardlink":
        assert outside.read_text(encoding="utf-8") == "outside-canary\n"
    runner.assert_not_called()


def test_tool_executor_rejects_workspace_root_replacement_before_runner(
    tmp_path,
):
    root = tmp_path / "root"
    detached = tmp_path / "detached"
    root.mkdir()
    agent = _agent(root)
    root.rename(detached)
    root.mkdir()
    (root / "README.md").write_text("replacement-canary\n", encoding="utf-8")
    runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = runner

    result = agent.execute_tool("read_file", {"path": "README.md"})

    assert result.metadata["tool_error_code"] == "workspace_entry_unsafe"
    runner.assert_not_called()


def test_tool_executor_patch_cas_reason_is_not_collapsed(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    agent = _agent(tmp_path)
    original_write = securitylib.write_regular_bytes_anchored_atomic
    calls = 0

    def external_change(*args, **kwargs):
        nonlocal calls
        calls += 1
        target.write_text("external-change\n", encoding="utf-8")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        securitylib,
        "write_regular_bytes_anchored_atomic",
        external_change,
    )

    result = agent.execute_tool(
        "patch_file",
        {"path": "note.txt", "old_text": "before", "new_text": "after"},
    )

    assert calls == 1
    assert result.metadata["tool_error_code"] == "workspace_changed_during_write"
    assert target.read_text(encoding="utf-8") == "external-change\n"


def test_read_file_rejects_more_than_200_requested_lines_before_runner(tmp_path):
    agent = _agent(tmp_path)
    runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = runner

    result = agent.execute_tool(
        "read_file",
        {"path": "README.md", "start": 1, "end": 201},
    )

    assert result.metadata["tool_error_code"] == "invalid_arguments"
    runner.assert_not_called()
