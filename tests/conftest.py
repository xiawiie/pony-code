from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat

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
