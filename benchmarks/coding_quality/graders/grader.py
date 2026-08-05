#!/usr/bin/env python3
"""Hidden deterministic graders for the coding-quality pilot corpus."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
import subprocess
import sys
import tempfile
from pathlib import Path


def _diagnosis_date_boundary():
    from date_ranges import inclusive_dates

    assert inclusive_dates(date(2026, 2, 1), date(2026, 2, 1)) == [date(2026, 2, 1)]
    assert inclusive_dates(date(2024, 2, 28), date(2024, 3, 1)) == [
        date(2024, 2, 28),
        date(2024, 2, 29),
        date(2024, 3, 1),
    ]


def _diagnosis_cache_key():
    from pricing import clear_cache, regional_price

    clear_cache()
    calls = []

    def loader(sku, region):
        calls.append((sku, region))
        return {"us": 10, "eu": 12}[region]

    assert regional_price("book", "us", loader) == 10
    assert regional_price("book", "eu", loader) == 12
    assert regional_price("book", "us", loader) == 10
    assert calls == [("book", "us"), ("book", "eu")]


def _navigation_unicode_config():
    from config_loader import load_name

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "name.txt"
        path.write_bytes(b"\xef\xbb\xbfPony\n")
        assert load_name(path) == "Pony"
        path.write_bytes(b"\xff\xfe")
        try:
            load_name(path)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid UTF-8 must raise ValueError")


def _navigation_nested_merge():
    from config_merge import merge_config

    base = {
        "service": {"host": "localhost", "tls": {"enabled": True, "port": 443}},
        "retries": 2,
    }
    overlay = {"service": {"tls": {"port": 8443}}, "retries": 4}
    assert merge_config(base, overlay) == {
        "service": {"host": "localhost", "tls": {"enabled": True, "port": 8443}},
        "retries": 4,
    }
    assert base["service"]["tls"]["port"] == 443


def _contract_runtime_option():
    from commands import build_command
    from options import RuntimeOptions

    assert RuntimeOptions().dry_run is False
    assert build_command("prod", RuntimeOptions(dry_run=True)) == "echo deploy prod"
    assert build_command("prod", RuntimeOptions(verbose=True, dry_run=True)) == (
        "echo deploy prod --verbose"
    )
    options = RuntimeOptions()
    try:
        options.dry_run = True
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("RuntimeOptions must remain frozen")


def _contract_error_envelope():
    from errors import AppError
    from response import handle

    def fail():
        raise AppError("permission_denied", "not allowed")

    assert handle(fail) == {
        "ok": False,
        "error": {"code": "permission_denied", "message": "not allowed"},
    }
    assert handle(lambda: 3) == {"ok": True, "value": 3}


def _hardening_path_traversal():
    from workspace_paths import resolve_workspace_path

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        assert resolve_workspace_path(root, "src/app.py") == root / "src/app.py"
        for value in ("../secret.txt", "src/../../secret.txt", str(root.parent / "x")):
            try:
                resolve_workspace_path(root, value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe path accepted: {value}")


def _hardening_env_update():
    from env_update import update_env

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        original = b"# provider\nMODEL=old\n"
        path.write_bytes(original)
        for assignments in (
            ["MODEL=one", "MODEL=two"],
            ["MODEL=new", "bad-key=value"],
        ):
            try:
                update_env(path, assignments)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid assignments must be rejected")
            assert path.read_bytes() == original
        update_env(path, ["MODEL=new", "REGION=us"])
        assert path.read_bytes() == b"# provider\nMODEL=new\nREGION=us\n"


GRADER_CASES = {
    "diagnosis-date-boundary": _diagnosis_date_boundary,
    "diagnosis-cache-key": _diagnosis_cache_key,
    "navigation-unicode-config": _navigation_unicode_config,
    "navigation-nested-merge": _navigation_nested_merge,
    "contract-runtime-option": _contract_runtime_option,
    "contract-error-envelope": _contract_error_envelope,
    "hardening-path-traversal": _hardening_path_traversal,
    "hardening-env-update": _hardening_env_update,
}


def _run_tests(pattern):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", pattern, "-q"],
        cwd=Path.cwd(),
        check=False,
    ).returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in GRADER_CASES or argv[1] not in {
        "target",
        "regression",
        "public",
    }:
        return 2
    sys.path.insert(0, str(Path.cwd()))
    if argv[1] == "public":
        return _run_tests("test_*.py")
    if argv[1] == "regression":
        pattern = "test_regression.py" if argv[0].startswith("diagnosis-") else "test_*.py"
        return _run_tests(pattern)
    try:
        GRADER_CASES[argv[0]]()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
