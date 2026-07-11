import os
import stat
import threading
import time
from pathlib import Path

import pytest

from pico import file_lock


def test_locked_file_serializes_overlapping_threads(tmp_path):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    lock_path = tmp_path / "store.lock"
    events = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def first_writer():
        with file_lock.locked_file(lock_path):
            events.append("first-entered")
            first_entered.set()
            release_first.wait(timeout=5)
            events.append("first-leaving")

    def second_writer():
        first_entered.wait(timeout=5)
        with file_lock.locked_file(lock_path):
            events.append("second-entered")

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    assert "second-entered" not in events

    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert events == ["first-entered", "first-leaving", "second-entered"]


def test_locked_file_creates_private_regular_file(tmp_path):
    lock_path = tmp_path / "store.lock"

    with file_lock.locked_file(lock_path):
        assert lock_path.is_file()

    if os.name == "posix":
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_locked_file_rejects_leaf_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    lock_path = tmp_path / "store.lock"
    lock_path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        with file_lock.locked_file(lock_path):
            raise AssertionError("lock yielded through a symlink")

    assert target.read_text(encoding="utf-8") == "untouched"


def test_locked_file_rejects_hardlink_without_chmod(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    target.chmod(0o644)
    lock_path = tmp_path / "store.lock"
    os.link(target, lock_path)

    with pytest.raises(ValueError, match="link|private"):
        with file_lock.locked_file(lock_path):
            raise AssertionError("lock yielded through a hardlink")

    assert target.read_text(encoding="utf-8") == "untouched"
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_locked_file_required_lock_fails_closed_without_fcntl(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)

    with pytest.raises(RuntimeError, match="lock unavailable"):
        with file_lock.locked_file(tmp_path / "required.lock", require_lock=True):
            raise AssertionError("required lock yielded without fcntl")


def test_locked_file_required_lock_fails_closed_when_flock_fails(tmp_path, monkeypatch):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    def fail_flock(fd, operation):
        raise OSError("flock failed")

    monkeypatch.setattr(file_lock.fcntl, "flock", fail_flock)

    with pytest.raises(OSError, match="flock failed"):
        with file_lock.locked_file(tmp_path / "required.lock", require_lock=True):
            raise AssertionError("required lock yielded after flock failure")


def test_locked_file_rejects_parent_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        with file_lock.locked_file(linked / "store.lock", require_lock=True):
            raise AssertionError("symlinked parent lock yielded")


def test_locked_file_rejects_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    fifo = tmp_path / "store.lock"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="regular"):
        with file_lock.locked_file(fifo, require_lock=True):
            raise AssertionError("FIFO lock yielded")


def test_locked_file_detects_inode_replacement_before_open(tmp_path, monkeypatch):
    lock_path = tmp_path / "store.lock"
    lock_path.write_text("original", encoding="utf-8")
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("replacement", encoding="utf-8")
    real_open = os.open
    swapped = False

    def replace_then_open(path, flags, mode=0o777, **kwargs):
        nonlocal swapped
        if Path(path).name == lock_path.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            lock_path.unlink()
            replacement.replace(lock_path)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(file_lock.os, "open", replace_then_open)
    with pytest.raises(ValueError, match="inode_changed"):
        with file_lock.locked_file(lock_path, require_lock=True):
            raise AssertionError("replaced lock yielded")


def test_locked_file_hardens_existing_regular_file(tmp_path):
    lock_path = tmp_path / "store.lock"
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o644)
    with file_lock.locked_file(lock_path, require_lock=True):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_locked_file_parent_swap_cannot_redirect_lock(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "parent-original"
    real_ensure = file_lock.ensure_private_dir
    swapped = False

    def ensure_then_swap(path):
        nonlocal swapped
        result = real_ensure(path)
        if Path(path) == parent and not swapped:
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(file_lock, "ensure_private_dir", ensure_then_swap)

    with pytest.raises((OSError, ValueError)):
        with file_lock.locked_file(parent / "store.lock", require_lock=True):
            raise AssertionError("redirected lock yielded")

    assert list(outside.iterdir()) == []
