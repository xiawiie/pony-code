from types import SimpleNamespace

from pony.tools import subprocess as hardened_subprocess
from pony.tools.subprocess import build_trusted_executables, run_process_group


def test_approved_shell_runner_returns_structured_process_result(tmp_path):
    python = build_trusted_executables(tmp_path, names=("python3",))["python3"]
    result = run_process_group(
        [python, "-c", "print('ok')"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=5,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False


def test_windows_python3_alias_reuses_trusted_python(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hardened_subprocess,
        "os",
        SimpleNamespace(
            name="nt",
            path=SimpleNamespace(abspath=lambda value: value),
        ),
    )
    monkeypatch.setattr(
        hardened_subprocess,
        "_safe_path_dirs",
        lambda _root, _env: [r"C:\Trusted"],
    )
    monkeypatch.setattr(
        hardened_subprocess.shutil,
        "which",
        lambda name, *, path: r"C:\Trusted\python.exe" if name == "python" else None,
    )
    monkeypatch.setattr(
        hardened_subprocess,
        "_verified_executable_identity",
        lambda _path: (1, b"python"),
    )

    executables = build_trusted_executables(
        tmp_path,
        names=("python", "python3"),
    )

    assert executables["python3"] is executables["python"]
