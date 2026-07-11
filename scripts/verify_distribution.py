#!/usr/bin/env python3
"""Verify Pico distribution contents and optionally smoke-test an installed wheel."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import venv
import zipfile


PROJECT_NAME = "pico"
PROJECT_VERSION = "0.1.0"
PROJECT_SUMMARY = (
    "Small local coding agent for DeepSeek, OpenAI-compatible, "
    "Anthropic-compatible, and Ollama models"
)
EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "top_level.txt",
}
DIST_INFO_FILES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
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
    output = _run("git", "ls-files", "--", "pico", cwd=repo)
    files = {line for line in output.splitlines() if line}
    if not files or any(not name.endswith(".py") for name in files):
        raise AssertionError(f"unexpected tracked package files: {sorted(files)}")
    return files


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

    egg_info = {f"pico.egg-info/{name}" for name in EGG_INFO_FILES}
    expected = tracked_package_files | {
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "setup.cfg",
    } | egg_info
    if files != expected:
        raise AssertionError(
            f"sdist file mismatch\nmissing: {sorted(expected - files)}\n"
            f"extra: {sorted(files - expected)}"
        )


def _metadata(headers: bytes):
    return BytesParser().parsebytes(headers)


def verify_wheel(wheel: Path, tracked_package_files: set[str], readme: str) -> None:
    dist_info = f"{PROJECT_NAME}-{PROJECT_VERSION}.dist-info"
    expected = tracked_package_files | {
        f"{dist_info}/{name}" for name in DIST_INFO_FILES
    }
    with zipfile.ZipFile(wheel) as archive:
        files = {name.rstrip("/") for name in archive.namelist() if not name.endswith("/")}
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
    assert metadata["Requires-Python"] == ">=3.11"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert metadata.get_all("Requires-Dist") is None
    assert metadata_body.decode("utf-8").strip() == readme.strip()
    assert entry_points == "[console_scripts]\npico = pico.cli:main\n"
    assert wheel_metadata["Root-Is-Purelib"] == "true"
    assert wheel_metadata.get_all("Tag") == ["py3-none-any"]


def install_smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pico-wheel-smoke-") as raw_tmp:
        root = Path(raw_tmp)
        environment = root / "venv"
        home = root / "home"
        cwd = root / "empty-project"
        home.mkdir()
        cwd.mkdir()
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)

        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        pico = bin_dir / ("pico.exe" if os.name == "nt" else "pico")
        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith("PICO_") or name in {
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
            }:
                env.pop(name)
        env["HOME"] = str(home)
        env["PATH"] = os.pathsep.join((str(bin_dir), env.get("PATH", "")))

        _run(str(python), "-m", "pip", "install", "--no-deps", str(wheel.resolve()), env=env)
        _run(str(python), "-m", "pip", "check", env=env)
        resolved = _run("/bin/sh", "-c", "command -v pico", cwd=cwd, env=env).strip()
        assert Path(resolved).resolve() == pico.resolve()
        _run(
            str(python),
            "-c",
            "import importlib.metadata as m; assert m.requires('pico') in (None, [])",
            cwd=cwd,
            env=env,
        )
        _run(str(pico), "--help", cwd=cwd, env=env)
        _run(str(pico), "doctor", "--offline", cwd=cwd, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--install-smoke", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    dist_dir = args.dist_dir.resolve()
    sdist = _single_artifact(dist_dir, "*.tar.gz")
    wheel = _single_artifact(dist_dir, "*.whl")
    tracked = _tracked_package_files(repo)
    verify_sdist(sdist, tracked)
    verify_wheel(wheel, tracked, (repo / "README.md").read_text(encoding="utf-8"))
    if args.install_smoke:
        install_smoke(wheel)
    print(f"verified {sdist.name} and {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
