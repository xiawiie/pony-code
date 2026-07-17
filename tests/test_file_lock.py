import os
import errno
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pony.state import file_lock


@pytest.fixture(autouse=True)
def _isolated_lock_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "_authority_root", lambda: tmp_path / ".authority")


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


def test_locked_file_serializes_after_locked_path_is_replaced(tmp_path):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    lock_path = tmp_path / "store.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_errors = []

    def first_holder():
        with file_lock.locked_file(lock_path, require_lock=True):
            first_entered.set()
            release_first.wait(timeout=5)

    def second_holder():
        try:
            with file_lock.locked_file(
                lock_path,
                require_lock=True,
                lock_timeout=0.05,
            ):
                second_entered.set()
        except Exception as exc:
            second_errors.append(exc)

    first = threading.Thread(target=first_holder)
    second = threading.Thread(target=second_holder)
    try:
        first.start()
        assert first_entered.wait(timeout=5)
        lock_path.unlink()
        lock_path.touch(mode=0o600)
        second.start()
        second.join(timeout=5)

        assert not second.is_alive()
        assert not second_entered.is_set()
        assert len(second_errors) == 1
        assert isinstance(second_errors[0], TimeoutError)
        release_first.set()
        first.join(timeout=5)
        with file_lock.locked_file(
            lock_path,
            require_lock=True,
            lock_timeout=0.5,
        ):
            pass
    finally:
        release_first.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(timeout=5)


def test_locked_file_authority_is_reentrant_for_distinct_paths(tmp_path):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    with file_lock.locked_file(tmp_path / "outer.lock", require_lock=True):
        with file_lock.locked_file(tmp_path / "inner.lock", require_lock=True):
            pass


def test_locked_file_authority_is_private_and_not_filesystem_root(
    tmp_path,
    monkeypatch,
):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    observed = []
    real_acquire = file_lock._acquire_flock

    def observe(descriptor, deadline):
        opened = os.fstat(descriptor)
        observed.append(
            (
                (opened.st_dev, opened.st_ino),
                stat.S_IMODE(opened.st_mode),
                opened.st_uid,
            )
        )
        return real_acquire(descriptor, deadline)

    monkeypatch.setattr(file_lock, "_acquire_flock", observe)
    lock_path = tmp_path / "store.lock"
    with file_lock.locked_file(lock_path, require_lock=True):
        pass

    authority = file_lock._authority_root().stat()
    filesystem_root = Path("/").stat()
    assert observed[0] == (
        (authority.st_dev, authority.st_ino),
        0o700,
        os.geteuid(),
    )
    assert observed[0][0] != (filesystem_root.st_dev, filesystem_root.st_ino)


def test_locked_file_rejects_authority_replaced_after_flock(
    tmp_path,
    monkeypatch,
):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    authority = file_lock._authority_root()
    lock_path = tmp_path / "store.lock"
    displaced = tmp_path / "authority-original"
    real_acquire = file_lock._acquire_flock
    swapped = False

    def swap_after_flock(descriptor, deadline):
        nonlocal swapped
        real_acquire(descriptor, deadline)
        if not swapped and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            authority.rename(displaced)
            authority.mkdir(mode=0o700)
            swapped = True

    monkeypatch.setattr(file_lock, "_acquire_flock", swap_after_flock)

    with pytest.raises(ValueError, match="unsafe lock authority"):
        with file_lock.locked_file(lock_path, require_lock=True):
            raise AssertionError("replaced authority yielded")

    assert swapped is True


def test_file_lock_import_does_not_resolve_home():
    script = (
        "from pathlib import Path\n"
        "Path.home = classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError('no home')))\n"
        "import pony.state.file_lock\n"
        "print('imported')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


@pytest.mark.parametrize("replace_parent", (False, True))
def test_authority_serializes_replacement_across_different_homes(
    tmp_path, replace_parent
):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    lock_path = store / "shared.lock"
    first_home = tmp_path / "home-one"
    second_home = tmp_path / "home-two"
    first_home.mkdir()
    second_home.mkdir()
    holder_script = (
        "import sys\n"
        "from pony.state.file_lock import locked_file\n"
        "with locked_file(sys.argv[1], require_lock=True):\n"
        "    print('entered', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    contender_script = (
        "import sys\n"
        "from pony.state.file_lock import locked_file\n"
        "try:\n"
        "    with locked_file(sys.argv[1], require_lock=True, lock_timeout=0.2):\n"
        "        print('entered')\n"
        "except TimeoutError:\n"
        "    print('timeout')\n"
    )
    first_env = {**os.environ, "HOME": str(first_home)}
    second_env = {**os.environ, "HOME": str(second_home)}
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lock_path)],
        cwd=Path.cwd(),
        env=first_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "entered"
        if replace_parent:
            store.rename(tmp_path / "store-original")
            store.mkdir(mode=0o700)
        else:
            lock_path.unlink()
        lock_path.touch(mode=0o600)

        blocked = subprocess.run(
            [sys.executable, "-c", contender_script, str(lock_path)],
            cwd=Path.cwd(),
            env=second_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "timeout"

        _remaining_stdout, holder_stderr = holder.communicate(
            "release\n",
            timeout=5,
        )
        assert holder.returncode == 0, holder_stderr

        released = subprocess.run(
            [sys.executable, "-c", contender_script, str(lock_path)],
            cwd=Path.cwd(),
            env=second_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert released.returncode == 0, released.stderr
        assert released.stdout.strip() == "entered"
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.communicate(timeout=5)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork unavailable")
def test_forked_child_closes_inherited_context_without_unlocking_parent(tmp_path):
    if file_lock.fcntl is None:
        pytest.skip("platform does not expose fcntl locks")

    lock_path = tmp_path / "fork.lock"
    script = (
        "import os, sys\n"
        "from pony.state.file_lock import locked_file\n"
        "child = False\n"
        "with locked_file(sys.argv[1], require_lock=True):\n"
        "    pid = os.fork()\n"
        "    child = pid == 0\n"
        "    if not child:\n"
        "        waited, status = os.waitpid(pid, 0)\n"
        "        assert waited == pid and os.waitstatus_to_exitcode(status) == 0\n"
        "if child:\n"
        "    os._exit(0)\n"
        "with locked_file(sys.argv[1], require_lock=True, lock_timeout=0.5):\n"
        "    print('parent-ok')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "parent-ok"


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


def test_locked_file_timeout_fails_without_yielding(tmp_path, monkeypatch):
    if file_lock.fcntl is None:
        pytest.skip("fcntl unavailable")

    monkeypatch.setattr(
        file_lock.fcntl,
        "flock",
        lambda _fd, _operation: (_ for _ in ()).throw(
            BlockingIOError(errno.EAGAIN, "busy")
        ),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        with file_lock.locked_file(
            tmp_path / "timeout.lock",
            require_lock=True,
            lock_timeout=0,
        ):
            raise AssertionError("timed out lock yielded")


def test_locked_file_real_process_contention_times_out_then_releases(tmp_path):
    if file_lock.fcntl is None:
        pytest.skip("fcntl unavailable")
    lock_path = tmp_path / "process.lock"
    script = (
        "import sys\n"
        "from pony.state.file_lock import locked_file\n"
        "with locked_file(sys.argv[1], require_lock=True):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=Path.cwd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError, match="timed out"):
            with file_lock.locked_file(
                lock_path,
                require_lock=True,
                lock_timeout=0.05,
            ):
                raise AssertionError("contended lock yielded")
        _remaining_stdout, child_stderr = child.communicate(
            "release\n",
            timeout=5,
        )
        assert child.returncode == 0, child_stderr
        with file_lock.locked_file(
            lock_path,
            require_lock=True,
            lock_timeout=0.5,
        ):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


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
