from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import stat
import threading

import pytest

from pony.security.private_files import (
    ensure_private_dir,
    ensure_private_file,
    private_directory_identity,
    private_file_signature,
)
from pony.security.paths import require_regular_no_symlink

SECRET_PATH_COMPONENT = "github_pat_A123456789012345678901234567890"


def test_private_identities_expose_platform_neutral_fields(tmp_path):
    root = ensure_private_dir(tmp_path / "state")
    path = root / "session.jsonl"
    path.write_bytes(b"{}\n")
    ensure_private_file(
        path,
        trusted_root=root,
        trusted_root_identity=private_directory_identity(root),
    )

    directory_identity = private_directory_identity(root)
    signature = private_file_signature(
        path,
        trusted_root=root,
        trusted_root_identity=directory_identity,
    )

    assert directory_identity.filesystem_id is not None
    assert directory_identity.file_id is not None
    assert signature.filesystem_id == directory_identity.filesystem_id
    assert signature.file_id is not None
    assert signature.size == 3
    assert signature.version_identity[:2] == (
        signature.filesystem_id,
        signature.file_id,
    )
    assert signature.is_private


def _ensure_private_dir_worker(path, start, queue):
    start.wait()
    try:
        queue.put(str(ensure_private_dir(path)))
    except BaseException as exc:
        queue.put(f"{type(exc).__name__}: {exc}")


def test_regular_guard_symlink_error_omits_sensitive_component(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / SECRET_PATH_COMPONENT
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError) as exc_info:
        require_regular_no_symlink(linked / "note.txt")

    assert str(exc_info.value) == "refusing symlink component"
    assert SECRET_PATH_COMPONENT not in str(exc_info.value)


def test_regular_guard_parent_type_error_omits_sensitive_component(tmp_path):
    parent = tmp_path / SECRET_PATH_COMPONENT
    parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        require_regular_no_symlink(parent / "note.txt")

    assert str(exc_info.value) == "parent component is not a directory"
    assert SECRET_PATH_COMPONENT not in str(exc_info.value)


def test_regular_guard_leaf_type_error_omits_sensitive_component(tmp_path):
    directory = tmp_path / SECRET_PATH_COMPONENT
    directory.mkdir()

    with pytest.raises(ValueError) as exc_info:
        require_regular_no_symlink(directory)

    assert str(exc_info.value) == "path is not a regular file"
    assert SECRET_PATH_COMPONENT not in str(exc_info.value)


def test_private_dir_symlink_error_omits_sensitive_component(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / SECRET_PATH_COMPONENT
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError) as exc_info:
        ensure_private_dir(linked / "nested")

    assert str(exc_info.value) == "private directory has symlink component"
    assert SECRET_PATH_COMPONENT not in str(exc_info.value)


def test_private_dir_unsafe_error_omits_sensitive_component(tmp_path):
    unsafe = tmp_path / SECRET_PATH_COMPONENT
    unsafe.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        ensure_private_dir(unsafe / "nested")

    assert str(exc_info.value) == "private directory has unsafe component"
    assert SECRET_PATH_COMPONENT not in str(exc_info.value)


def test_private_hardening_refuses_symlink_without_chmodding_target(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("sentinel", encoding="utf-8")
    before = stat.S_IMODE(outside.stat().st_mode)
    linked = tmp_path / "linked"
    linked.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        ensure_private_file(linked)

    assert stat.S_IMODE(outside.stat().st_mode) == before


def test_private_hardening_refuses_symlinked_parent(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir()
    linked_parent = tmp_path / "private"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        ensure_private_dir(linked_parent / "nested")

    assert not (outside / "nested").exists()


def test_regular_file_guard_refuses_symlinked_parent(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-read"
    outside.mkdir()
    (outside / "note.txt").write_text("outside", encoding="utf-8")
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        require_regular_no_symlink(tmp_path / "docs" / "note.txt")


@pytest.mark.parametrize("helper", (require_regular_no_symlink, ensure_private_file))
def test_file_helpers_refuse_symlink_deep_in_parent_chain(tmp_path, helper):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-deep-read"
    outside.mkdir()
    (outside / "nested").mkdir()
    target = outside / "nested" / "note.txt"
    target.write_text("outside", encoding="utf-8")
    before = stat.S_IMODE(target.stat().st_mode)
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir()
    (safe_parent / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        helper(safe_parent / "linked" / "nested" / "note.txt")

    assert stat.S_IMODE(target.stat().st_mode) == before


def test_private_directory_refuses_symlink_deep_in_parent_chain(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-deep-dir"
    outside.mkdir()
    (outside / "nested").mkdir()
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir()
    (safe_parent / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        ensure_private_dir(safe_parent / "linked" / "nested" / "private")

    assert not (outside / "nested" / "private").exists()


def test_private_modes_are_owner_only(tmp_path):
    directory = ensure_private_dir(tmp_path / "private")
    target = directory / "artifact.json"
    target.write_text("{}", encoding="utf-8")
    ensure_private_file(target)

    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_private_directory_creation_hardens_new_descendants_only(tmp_path):
    before = stat.S_IMODE(tmp_path.stat().st_mode)

    target = ensure_private_dir(tmp_path / "owned" / "nested")

    assert target == tmp_path / "owned" / "nested"
    if os.name == "posix":
        assert stat.S_IMODE(tmp_path.stat().st_mode) == before
        assert stat.S_IMODE((tmp_path / "owned").stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_directory_concurrent_thread_creation_handles_file_exists(
    tmp_path, monkeypatch
):
    target = tmp_path / "shared"
    barrier = threading.Barrier(2)
    mkdir = Path.mkdir

    def synchronized_mkdir(path, *args, **kwargs):
        if path == target:
            barrier.wait(timeout=5)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", synchronized_mkdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(ensure_private_dir, (target, target)))

    assert results == (target, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_directory_concurrent_process_creation(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    target = tmp_path / "shared" / "nested"
    processes = [
        context.Process(
            target=_ensure_private_dir_worker,
            args=(target, start, queue),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert [queue.get(timeout=1) for _ in processes] == [str(target)] * len(processes)
    assert stat.S_IMODE((tmp_path / "shared").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes required")
def test_private_directory_creation_sets_exact_modes_with_restrictive_umask(tmp_path):
    previous_umask = os.umask(0o777)
    try:
        target = ensure_private_dir(tmp_path / "owned" / "nested")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((tmp_path / "owned").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_directory_does_not_chmod_existing_ancestor(tmp_path):
    ancestor = tmp_path / "external"
    ancestor.mkdir(mode=0o755)
    if os.name == "posix":
        ancestor.chmod(0o755)

    target = ensure_private_dir(ancestor / "owned")

    if os.name == "posix":
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_regular_file_guard_allows_only_missing_leaf(tmp_path):
    missing = tmp_path / "missing.txt"

    assert require_regular_no_symlink(missing, allow_missing=True) == missing
    with pytest.raises(FileNotFoundError):
        require_regular_no_symlink(
            tmp_path / "missing-parent" / "file.txt", allow_missing=True
        )


def test_regular_file_guard_rejects_non_regular_leaf(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        require_regular_no_symlink(directory)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_regular_file_guard_rejects_fifo_without_opening_it(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="regular file"):
        require_regular_no_symlink(fifo)


def test_windows_security_descriptor_matches_native_pointer_layout():
    import ctypes

    from pony.security.windows_native import _SecurityDescriptor

    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected_size = ((4 + 4 * pointer_size + pointer_size - 1) // pointer_size) * pointer_size
    assert ctypes.sizeof(_SecurityDescriptor) == expected_size


@pytest.mark.parametrize(
    "component",
    ("", ".", "..", "CON", "nul.txt", "name:stream", "trailing.", "trailing "),
)
def test_windows_private_path_component_rejects_ambiguous_names(component):
    from pony.security.windows_native import lexical_component

    with pytest.raises(ValueError, match="unsafe Windows path component"):
        lexical_component(component)


def test_windows_private_path_component_accepts_unicode_name():
    from pony.security.windows_native import lexical_component

    assert lexical_component("会话-01.jsonl") == "会话-01.jsonl"


def test_windows_error_code_falls_back_when_winerror_is_none():
    from pony.security.windows_native import error_code

    error = FileNotFoundError(2, "missing")
    error.winerror = None

    assert error_code(error) == 2


@pytest.mark.parametrize(
    ("absolute", "expected"),
    (
        (r"C:\\work\\pony", r"\\?\C:\\work\\pony"),
        (r"\\server\share\\pony", r"\\?\UNC\server\share\\pony"),
        (r"\\?\C:\\work\\pony", r"\\?\C:\\work\\pony"),
    ),
)

def test_windows_native_path_uses_extended_length_namespace(
    absolute, expected, monkeypatch
):
    from pony.security import windows_native

    monkeypatch.setattr(windows_native.os.path, "abspath", lambda _path: absolute)
    assert windows_native._win32_path("ignored") == expected

def test_windows_directory_mutability_ignores_attribute_only_access(monkeypatch):
    from pony.security import windows_native

    attempted = []

    def deny_access(_path, *, desired_access, **_kwargs):
        attempted.append(desired_access)
        raise OSError(5, "access denied")

    monkeypatch.setattr(windows_native, "open_path", deny_access)

    assert windows_native.path_is_mutable_by_current_user(
        r"C:\Windows", directory=True
    ) is False
    assert windows_native._FILE_WRITE_ATTRIBUTES not in attempted
    assert windows_native._DELETE in attempted


def test_windows_volume_root_mutability_skips_delete_access(monkeypatch):
    from pony.security import windows_native

    attempted = []

    def deny_access(_path, *, desired_access, **_kwargs):
        attempted.append(desired_access)
        raise OSError(5, "access denied")

    monkeypatch.setattr(windows_native, "open_path", deny_access)
    assert windows_native.path_is_mutable_by_current_user(
        Path("/"), directory=True
    ) is False
    assert windows_native._DELETE not in attempted
    assert windows_native._FILE_DELETE_CHILD in attempted


def test_windows_file_mutability_keeps_attribute_access_check(monkeypatch):
    from pony.security import windows_native

    attempted = []

    def allow_access(_path, *, desired_access, **_kwargs):
        attempted.append(desired_access)

        class Handle:
            def close(self):
                pass

        return Handle()

    monkeypatch.setattr(windows_native, "open_path", allow_access)

    assert windows_native.path_is_mutable_by_current_user(
        r"C:\Windows\powershell.exe", directory=False
    ) is True
    assert attempted == [windows_native._FILE_WRITE_ATTRIBUTES]
