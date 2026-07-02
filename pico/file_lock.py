"""Small cross-process file lock helper."""

from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - fcntl is unavailable on some platforms.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@contextmanager
def locked_file(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
