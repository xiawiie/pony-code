import os
import stat
import threading
import time

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
