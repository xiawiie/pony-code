from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

import pytest

from pony.tools import subprocess as safe_subprocess_module


_REAL_HOME = Path.home()


@pytest.fixture
def real_home():
    """Opt out of test HOME isolation for explicit host integration tests."""
    return _REAL_HOME


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch, request):
    """Keep test-created Pony state out of the user's HOME."""
    if "real_home" in request.fixturenames:
        return _REAL_HOME
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        Path,
        "home",
        staticmethod(lambda: Path(os.environ.get("HOME", home))),
    )
    return home


_WINDOWS_SYMLINK_SKIP_REASON = (
    "Windows symlink attack fixture requires Developer Mode or "
    "SeCreateSymbolicLinkPrivilege"
)


@lru_cache(maxsize=1)
def _windows_test_symlink_unavailable() -> bool:
    with tempfile.TemporaryDirectory(prefix="pony-symlink-probe-") as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("probe", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError as exc:
            return getattr(exc, "winerror", None) in {5, 1314}
        return False


def _is_unavailable_test_symlink(excinfo) -> bool:
    if os.name != "nt" or not isinstance(excinfo.value, OSError):
        return False
    if (
        getattr(excinfo.value, "winerror", None) not in {5, 1314}
        or not _windows_test_symlink_unavailable()
    ):
        return False

    repository_root = Path(__file__).resolve().parents[1]
    repository_frames = []
    for entry in excinfo.traceback:
        try:
            relative = Path(str(entry.path)).resolve().relative_to(repository_root)
        except ValueError:
            continue
        repository_frames.append((relative, entry.statement))

    if not repository_frames:
        return False
    frame, statement = repository_frames[-1]
    if statement is None or "symlink" not in str(statement):
        return False
    return frame.parts[0] == "tests" or frame.parts[:3] == (
        "benchmarks",
        "live_e2e",
        "tests",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Skip only unavailable Windows symlink attack fixtures, not product failures."""
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is None or not _is_unavailable_test_symlink(call.excinfo):
        return
    report.outcome = "skipped"
    report.longrepr = (str(item.path), report.location[1], _WINDOWS_SYMLINK_SKIP_REASON)


@pytest.fixture
def contract_git(monkeypatch):
    """Run real-Git behavior contracts independently from host path ownership."""
    discovered = shutil.which("git")
    if not discovered:
        pytest.fail("Git is required for the real Git contract tests")
    executable = Path(discovered).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or not executable.is_file():
        pytest.fail("Git test dependency is not a regular file")

    original = safe_subprocess_module._prepared_executable

    @contextmanager
    def prepare(candidate):
        if Path(candidate).resolve(strict=True) == executable:
            yield safe_subprocess_module._PreparedExecutable(
                str(executable), str(executable)
            )
            return
        with original(candidate) as prepared:
            yield prepared

    # Discovery/path immutability has dedicated tests. These contracts exercise
    # hardened Git argv, environment, repository validation, and semantics.
    monkeypatch.setattr(safe_subprocess_module, "_prepared_executable", prepare)
    return str(executable)


@pytest.fixture
def contract_python(monkeypatch):
    """Run Python behavior contracts independently from host path ownership."""
    executable = Path(sys.executable).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or not executable.is_file():
        pytest.fail("Python test dependency is not a regular file")

    original = safe_subprocess_module._prepared_executable

    @contextmanager
    def prepare(candidate):
        if Path(candidate).resolve(strict=True) == executable:
            yield safe_subprocess_module._PreparedExecutable(
                str(executable), str(executable)
            )
            return
        with original(candidate) as prepared:
            yield prepared

    # Discovery/path immutability has dedicated tests. These contracts exercise
    # Python argv, process control, verifier, and grader behavior.
    monkeypatch.setattr(safe_subprocess_module, "_prepared_executable", prepare)
    return str(executable)


@pytest.fixture
def contract_rg(monkeypatch):
    """Run real-rg contracts independently from host path ownership."""
    discovered = shutil.which("rg")
    if not discovered:
        pytest.fail("ripgrep is required for the real rg contract tests")
    executable = Path(discovered).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or not executable.is_file():
        pytest.fail("ripgrep test dependency is not a regular file")

    original = safe_subprocess_module._prepared_executable

    @contextmanager
    def prepare(candidate):
        if Path(candidate).resolve(strict=True) == executable:
            yield safe_subprocess_module._PreparedExecutable(
                str(executable), str(executable)
            )
            return
        with original(candidate) as prepared:
            yield prepared

    # Discovery/path immutability has dedicated tests. These contracts exercise
    # rg argv, environment, filtering, and semantics with the real binary.
    monkeypatch.setattr(safe_subprocess_module, "_prepared_executable", prepare)
    return str(executable)
