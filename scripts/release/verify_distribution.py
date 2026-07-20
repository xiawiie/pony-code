#!/usr/bin/env python3
"""Verify Pony distribution contents and optionally smoke-test an installed wheel."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile


_REPO = Path(__file__).resolve().parents[2]
_PROJECT = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]
PROJECT_NAME = _PROJECT["name"]
DIST_INFO_NAME = PROJECT_NAME.replace("-", "_")
PROJECT_VERSION = _PROJECT["version"]
PROJECT_SUMMARY = _PROJECT["description"]
EXPECTED_REQUIRES_PYTHON = "<3.13,>=3.11"
EXPECTED_RUNTIME_REQUIREMENTS = ["prompt-toolkit<4,>=3.0.52"]
DIST_INFO_FILES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
}


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def _tracked_package_files(repo: Path) -> set[str]:
    output = _run("git", "ls-files", "--", "pony", cwd=repo)
    source_files = {
        line for line in output.splitlines() if line and os.path.lexists(repo / line)
    }
    files = source_files
    if not files or any(not name.endswith(".py") for name in files):
        raise AssertionError(f"unexpected tracked package files: {sorted(files)}")
    source_python = {
        path.relative_to(repo).as_posix()
        for path in (repo / "pony").rglob("*.py")
        if path.is_file()
    }
    untracked_python = source_python - files
    if untracked_python:
        raise AssertionError(
            f"untracked package Python files: {sorted(untracked_python)}"
        )
    return files


def _runtime_package_files(repo: Path, tracked_package_files: set[str]) -> set[str]:
    del repo
    return set(tracked_package_files)


def _single_artifact(dist_dir: Path, pattern: str) -> Path:
    matches = sorted(dist_dir.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"expected one {pattern} in {dist_dir}, found {matches}")
    return matches[0]


def verify_sdist(sdist: Path, tracked_package_files: set[str]) -> None:
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise AssertionError("sdist contains a link or special file")
        roots = {PurePosixPath(member.name).parts[0] for member in members}
        if len(roots) != 1:
            raise AssertionError(f"sdist must have one wrapper directory: {roots}")
        wrapper = roots.pop()
        files = {
            PurePosixPath(member.name).relative_to(wrapper).as_posix()
            for member in members
            if member.isfile()
        }

    expected = (
        tracked_package_files
        | {
            ".gitignore",
            "PKG-INFO",
            "LICENSE",
            "README.md",
            "pyproject.toml",
        }
    )
    if files != expected:
        raise AssertionError(
            f"sdist file mismatch\nmissing: {sorted(expected - files)}\n"
            f"extra: {sorted(files - expected)}"
        )


def _metadata(headers: bytes):
    return BytesParser().parsebytes(headers)


def verify_wheel(wheel: Path, tracked_package_files: set[str], readme: str) -> None:
    dist_info = f"{DIST_INFO_NAME}-{PROJECT_VERSION}.dist-info"
    expected = tracked_package_files | {
        f"{dist_info}/{name}" for name in DIST_INFO_FILES
    }
    with zipfile.ZipFile(wheel) as archive:
        files = {
            name.rstrip("/") for name in archive.namelist() if not name.endswith("/")
        }
        if files != expected:
            raise AssertionError(
                f"wheel file mismatch\nmissing: {sorted(expected - files)}\n"
                f"extra: {sorted(files - expected)}"
            )
        metadata_bytes = archive.read(f"{dist_info}/METADATA")
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode()
        wheel_metadata = _metadata(archive.read(f"{dist_info}/WHEEL"))

    metadata_headers, separator, metadata_body = metadata_bytes.partition(b"\n\n")
    assert separator
    metadata = _metadata(metadata_headers + b"\n\n")
    assert metadata["Name"] == PROJECT_NAME
    assert metadata["Version"] == PROJECT_VERSION
    assert metadata["Summary"] == PROJECT_SUMMARY
    assert metadata["Requires-Python"] == EXPECTED_REQUIRES_PYTHON
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("License-File") == ["LICENSE"]
    assert metadata.get_all("Project-URL") == [
        "Homepage, https://github.com/xiawiie/pony-code",
        "Documentation, https://github.com/xiawiie/pony-code#readme",
        "Issues, https://github.com/xiawiie/pony-code/issues",
        "Source, https://github.com/xiawiie/pony-code",
    ]
    assert metadata.get_all("Requires-Dist") == EXPECTED_RUNTIME_REQUIREMENTS
    assert metadata_body.decode("utf-8").strip() == readme.strip()
    assert entry_points == "[console_scripts]\npony = pony.cli.app:main\n"
    assert wheel_metadata["Root-Is-Purelib"] == "true"
    assert wheel_metadata.get_all("Tag") == ["py3-none-any"]


def _smoke_env(home: Path, bin_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("PONY_") or name in {
            "PYTHONHOME",
            "PYTHONPATH",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
        }:
            env.pop(name)
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join((str(bin_dir), env.get("PATH", "")))
    return env


def _locked_runtime_requirements() -> tuple[str, ...]:
    lock = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    direct = packages[PROJECT_NAME].get("dependencies", [])
    return tuple(
        f"{dependency['name']}=={packages[dependency['name']]['version']}"
        for dependency in direct
    )


def _install_with_uv(python: Path, *requirements: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise AssertionError("uv is required for locked offline install smoke")
    _run(
        uv,
        "pip",
        "install",
        "--offline",
        "--python",
        str(python),
        *requirements,
    )


def install_smoke(wheel: Path, *, offline: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="pony-wheel-smoke-") as raw_tmp:
        root = Path(raw_tmp)
        environment = root / "venv"
        home = root / "home"
        cwd = root / "empty-project"
        home.mkdir()
        cwd.mkdir()
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)

        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        pony = bin_dir / ("pony.exe" if os.name == "nt" else "pony")
        env = _smoke_env(home, bin_dir)

        if offline:
            _install_with_uv(python, str(wheel.resolve()))
        else:
            _install_with_uv(python, *_locked_runtime_requirements())
            _run(
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                str(wheel.resolve()),
                env=env,
            )
        _run(str(python), "-m", "pip", "check", env=env)
        resolved = _run("/bin/sh", "-c", "command -v pony", cwd=cwd, env=env).strip()
        assert Path(resolved).resolve() == pony.resolve()
        _run(
            str(python),
            "-c",
            "import importlib.metadata as m; "
            "assert m.requires('pony-code') == ['prompt-toolkit<4,>=3.0.52']",
            cwd=cwd,
            env=env,
        )
        _run(
            str(python),
            "-c",
            "import prompt_toolkit; import pony.tui.app",
            cwd=cwd,
            env=env,
        )
        _run(
            str(python),
            "-c",
            "import importlib.util; "
            "forbidden=('pony.providers.fake','pony.providers.anthropic_compatible',"
            "'pony.providers.openai_compatible','pony.providers.openai_chat',"
            "'pony.providers.ollama'); "
            "assert all(importlib.util.find_spec(name) is None for name in forbidden); "
            "assert importlib.util.find_spec('benchmarks') is None",
            cwd=cwd,
            env=env,
        )
        _run(str(pony), "--help", cwd=cwd, env=env)
        installed_version = _run(str(pony), "--version", cwd=cwd, env=env).strip()
        assert installed_version == f"pony {PROJECT_VERSION}"
        _run(str(pony), "doctor", cwd=cwd, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--install-smoke", action="store_true")
    parser.add_argument("--offline-bundle-smoke", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    dist_dir = args.dist_dir.resolve()
    sdist = _single_artifact(dist_dir, "*.tar.gz")
    wheel = _single_artifact(dist_dir, "*.whl")
    tracked = _tracked_package_files(repo)
    runtime = _runtime_package_files(repo, tracked)
    verify_sdist(sdist, tracked)
    verify_wheel(wheel, runtime, (repo / "README.md").read_text(encoding="utf-8"))
    if args.install_smoke:
        install_smoke(wheel)
    if args.offline_bundle_smoke:
        install_smoke(wheel, offline=True)
    print(f"verified {sdist.name} and {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
