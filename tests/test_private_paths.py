from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import threading

import pytest

from pony.security.private_files import (
    ensure_private_dir,
    ensure_private_file,
    harden_private_tree,
    private_directory_identity,
    private_file_signature,
    write_private_bytes_atomic,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_harden_private_tree_supports_extended_length_paths(tmp_path):
    root = ensure_private_dir(tmp_path / "state")
    long_root = root.joinpath(
        *(f"component-{index}-" + "x" * 30 for index in range(7))
    )
    ensure_private_dir(long_root)
    root_identity = private_directory_identity(root)
    artifact = long_root / "artifact.json"
    write_private_bytes_atomic(
        artifact,
        b"{}",
        trusted_root=root,
        trusted_root_identity=root_identity,
    )

    harden_private_tree(root)

    assert private_file_signature(
        artifact,
        trusted_root=root,
        trusted_root_identity=root_identity,
    ).is_private


@pytest.mark.skipif(os.name != "nt", reason="Windows handle promotion contract")
def test_windows_private_promotion_uses_handle_authority(tmp_path, monkeypatch):
    from pony.security import windows_private_files

    root = ensure_private_dir(tmp_path / "state")
    root_identity = private_directory_identity(root)
    candidate = root / "candidate.jsonl"
    destination = root / "session.jsonl"
    write_private_bytes_atomic(
        candidate,
        b"new\n",
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    write_private_bytes_atomic(
        destination,
        b"old\n",
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    candidate_signature = private_file_signature(
        candidate,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    destination_signature = private_file_signature(
        destination,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )

    def reject_path_publish(*_args, **_kwargs):
        raise AssertionError("path-based publish is forbidden")

    monkeypatch.setattr(windows_private_files.native, "move_file", reject_path_publish)
    monkeypatch.setattr(
        windows_private_files.native,
        "replace_file",
        reject_path_publish,
    )

    windows_private_files.promote_private_file(
        candidate,
        destination,
        trusted_root=root,
        trusted_root_identity=root_identity,
        expected_source_identity=(
            candidate_signature.filesystem_id,
            candidate_signature.file_id,
        ),
        expected_destination_identity=(
            destination_signature.filesystem_id,
            destination_signature.file_id,
        ),
    )

    assert not candidate.exists()
    assert destination.read_bytes() == b"new\n"
    assert not tuple(root.glob("*.restore"))


@pytest.mark.skipif(os.name != "nt", reason="Windows handle promotion contract")
def test_windows_private_promotion_rolls_back_after_reported_install_failure(
    tmp_path,
    monkeypatch,
):
    from pony.security import windows_private_files

    root = ensure_private_dir(tmp_path / "state")
    root_identity = private_directory_identity(root)
    candidate = root / "candidate.jsonl"
    destination = root / "session.jsonl"
    write_private_bytes_atomic(
        candidate,
        b"new\n",
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    write_private_bytes_atomic(
        destination,
        b"old\n",
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    candidate_signature = private_file_signature(
        candidate,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    destination_signature = private_file_signature(
        destination,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    candidate_identity = (
        candidate_signature.filesystem_id,
        candidate_signature.file_id,
    )
    original_rename = windows_private_files.native.rename_handle
    failed = False

    def fail_after_install(handle, parent, name, **kwargs):
        nonlocal failed
        result = original_rename(handle, parent, name, **kwargs)
        if (
            not failed
            and name == destination.name
            and windows_private_files.native.identity(handle) == candidate_identity
        ):
            failed = True
            raise OSError("candidate publish crash")
        return result

    monkeypatch.setattr(
        windows_private_files.native,
        "rename_handle",
        fail_after_install,
    )

    with pytest.raises(OSError, match="publish crash"):
        windows_private_files.promote_private_file(
            candidate,
            destination,
            trusted_root=root,
            trusted_root_identity=root_identity,
            expected_source_identity=candidate_identity,
            expected_destination_identity=(
                destination_signature.filesystem_id,
                destination_signature.file_id,
            ),
        )

    assert candidate.read_bytes() == b"new\n"
    assert destination.read_bytes() == b"old\n"
    assert not tuple(root.glob("*.restore"))


def _ensure_private_dir_worker(path, start, queue):
    start.wait()
    try:
        queue.put(str(ensure_private_dir(path)))
    except BaseException as exc:
        queue.put(f"{type(exc).__name__}: {exc}")


def _assert_private_directory(path):
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        return
    from pony.security import windows_native

    with windows_native.open_path(path, directory=True) as handle:
        windows_native.require_private(handle)


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
    if os.name == "nt":
        from pony.security import windows_native

        open_relative = windows_native.open_relative

        def synchronized_open_relative(root, name, *args, **kwargs):
            if (
                name == target.name
                and kwargs.get("disposition") == windows_native.FILE_CREATE
            ):
                barrier.wait(timeout=5)
            return open_relative(root, name, *args, **kwargs)

        monkeypatch.setattr(
            windows_native, "open_relative", synchronized_open_relative
        )
    else:
        mkdir = Path.mkdir

        def synchronized_mkdir(path, *args, **kwargs):
            if path == target:
                barrier.wait(timeout=5)
            return mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", synchronized_mkdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(ensure_private_dir, (target, target)))

    assert results == (target, target)
    _assert_private_directory(target)


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
    _assert_private_directory(tmp_path / "shared")
    _assert_private_directory(target)


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


@pytest.mark.parametrize(
    ("initially_private", "expected"),
    (
        (True, ["require"]),
        (False, ["require", "make", "require"]),
    ),
)
def test_windows_private_hardening_is_idempotent(
    initially_private, expected, monkeypatch
):
    from pony.security import windows_private_files

    events = []

    def require_private(_handle):
        events.append("require")
        if not initially_private and events == ["require"]:
            raise ValueError("private file permissions are unsafe")

    monkeypatch.setattr(windows_private_files.native, "require_private", require_private)
    monkeypatch.setattr(
        windows_private_files.native,
        "make_private",
        lambda _handle: events.append("make"),
    )

    windows_private_files._harden_private(object())

    assert events == expected


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


def test_windows_reparse_rejection_is_typed_and_platform_neutral(monkeypatch):
    from pony.security import windows_native

    reparse_facts = type("ReparseFacts", (), {"reparse_tag": 0xA000000C})()
    monkeypatch.setattr(windows_native, "facts", lambda _handle: reparse_facts)

    with pytest.raises(windows_native.ReparsePointError) as exc_info:
        windows_native.require_kind(object(), directory=False)

    assert str(exc_info.value) == "refusing symlink component"


def test_windows_private_dir_normalizes_reparse_error(monkeypatch):
    from pony.security import windows_native, windows_private_files

    def reject_reparse(_path):
        raise windows_native.ReparsePointError()

    monkeypatch.setattr(windows_private_files.native, "ensure_directory", reject_reparse)

    with pytest.raises(ValueError) as exc_info:
        windows_private_files.ensure_private_dir("ignored")

    assert type(exc_info.value) is ValueError
    assert str(exc_info.value) == "private directory has symlink component"


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


@pytest.mark.parametrize("replace", (False, True))
def test_windows_handle_rename_uses_relative_nt_file_information(monkeypatch, replace):
    from pony.security import windows_native

    observed = {}

    class Ntdll:
        @staticmethod
        def NtSetInformationFile(handle, _status, buffer, size, info_class):
            observed.update(
                handle=handle,
                buffer=bytes(buffer.raw[:size]),
                size=size,
                info_class=info_class,
            )
            return 0

    monkeypatch.setattr(
        windows_native,
        "api",
        lambda: type("Api", (), {"ntdll": Ntdll()})(),
    )

    windows_native.rename_handle(
        type("Handle", (), {"value": 11})(),
        type("Handle", (), {"value": 22})(),
        "renamed",
        replace=replace,
    )

    info = windows_native._FileRenameInfo.from_buffer_copy(observed["buffer"])
    name = "renamed".encode("utf-16-le")
    start = windows_native._FileRenameInfo.FileName.offset
    assert bool(info.ReplaceIfExists) is replace
    assert info.RootDirectory == 22
    assert info.FileNameLength == len(name)
    assert observed["buffer"][start : start + len(name)] == name
    assert observed["info_class"] == windows_native._FILE_RENAME_INFORMATION


@pytest.mark.parametrize("posix", (False, True))
def test_windows_handle_delete_selects_posix_disposition(monkeypatch, posix):
    from pony.security import windows_native

    observed = {}

    class Kernel32:
        @staticmethod
        def SetFileInformationByHandle(handle, info_class, info, size):
            observed.update(
                handle=handle,
                info_class=info_class,
                buffer=windows_native.ctypes.string_at(info, size),
            )
            return True

    monkeypatch.setattr(
        windows_native,
        "api",
        lambda: type("Api", (), {"kernel32": Kernel32()})(),
    )

    windows_native.delete_handle(type("Handle", (), {"value": 11})(), posix=posix)

    if posix:
        info = windows_native._FileDispositionInfoEx.from_buffer_copy(
            observed["buffer"]
        )
        assert info.Flags == (
            windows_native._FILE_DISPOSITION_FLAG_DELETE
            | windows_native._FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
        )
        assert observed["info_class"] == windows_native._FILE_DISPOSITION_INFO_EX_CLASS
    else:
        info = windows_native._FileDispositionInfo.from_buffer_copy(observed["buffer"])
        assert bool(info.DeleteFile)
        assert observed["info_class"] == windows_native._FILE_DISPOSITION_INFO_CLASS


def _mock_windows_mutability_access(monkeypatch, windows_native, masks):
    opened = []
    remaining = iter(masks)

    class Handle:
        def __init__(self, path):
            self.path = Path(path)
            self.closed = False

        def close(self):
            self.closed = True

    def open_read_only(path, **kwargs):
        assert "desired_access" not in kwargs
        assert kwargs == {
            "directory": Path(path).suffix.casefold() != ".exe",
            "single_link": False,
        }
        handle = Handle(path)
        opened.append(handle)
        return handle

    monkeypatch.setattr(windows_native, "open_path", open_read_only)
    monkeypatch.setattr(
        windows_native,
        "_effective_access_mask",
        lambda handle: next(remaining),
    )
    return opened


def test_windows_directory_mutability_ignores_attribute_only_access(monkeypatch):
    from pony.security import windows_native

    opened = _mock_windows_mutability_access(
        monkeypatch,
        windows_native,
        (windows_native._FILE_WRITE_ATTRIBUTES, 0),
    )
    path = Path(r"C:\Windows\System32")

    assert windows_native.path_is_mutable_by_current_user(
        path, directory=True
    ) is False
    assert [handle.path for handle in opened] == [path, path.parent]
    assert all(handle.closed for handle in opened)


def test_windows_volume_root_mutability_skips_delete_access(monkeypatch):
    from pony.security import windows_native

    opened = _mock_windows_mutability_access(
        monkeypatch,
        windows_native,
        (windows_native._DELETE,),
    )
    root = Path(Path.cwd().anchor)

    assert windows_native.path_is_mutable_by_current_user(
        root, directory=True
    ) is False
    assert [handle.path for handle in opened] == [root]
    assert opened[0].closed is True


def test_windows_file_mutability_keeps_attribute_access_check(monkeypatch):
    from pony.security import windows_native

    opened = _mock_windows_mutability_access(
        monkeypatch,
        windows_native,
        (windows_native._FILE_WRITE_ATTRIBUTES,),
    )
    path = Path(r"C:\Windows\powershell.exe")

    assert windows_native.path_is_mutable_by_current_user(
        path, directory=False
    ) is True
    assert [handle.path for handle in opened] == [path]
    assert opened[0].closed is True


def test_windows_parent_delete_child_makes_target_replaceable(monkeypatch):
    from pony.security import windows_native

    opened = _mock_windows_mutability_access(
        monkeypatch,
        windows_native,
        (0, windows_native._FILE_DELETE_CHILD),
    )
    path = Path(r"C:\Program Files\Git\cmd\git.exe")

    assert windows_native.path_is_mutable_by_current_user(
        path, directory=False
    ) is True
    assert [handle.path for handle in opened] == [path, path.parent]
    assert all(handle.closed for handle in opened)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable image locking contract")
def test_windows_running_executable_mutability_probe_uses_access_check():
    from pony.security import windows_native

    result = windows_native.path_is_mutable_by_current_user(
        sys.executable,
        directory=False,
    )

    assert isinstance(result, bool)
