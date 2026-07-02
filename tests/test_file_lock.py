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
