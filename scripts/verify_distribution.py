#!/usr/bin/env python3
"""Verify Pico distribution contents and optionally smoke-test an installed wheel."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile


_REPO = Path(__file__).resolve().parents[1]
_PROJECT = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]
PROJECT_NAME = _PROJECT["name"]
PROJECT_VERSION = _PROJECT["version"]
PROJECT_SUMMARY = _PROJECT["description"]
PACKAGE_DATA_FILES = {
    "pico/_docker_sandbox/image-manifest.json",
    "pico/_docker_sandbox/docker-config/config.json",
}
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
    files = {
        line
        for line in output.splitlines()
        if line and os.path.lexists(repo / line)
    }
    if not files or any(
        not name.endswith(".py") and name not in PACKAGE_DATA_FILES
        for name in files
    ):
        raise AssertionError(f"unexpected tracked package files: {sorted(files)}")
    untracked_package_data = PACKAGE_DATA_FILES - files
    if untracked_package_data:
        raise AssertionError(
            f"untracked package data files: {sorted(untracked_package_data)}"
        )
    source_python = {
        path.relative_to(repo).as_posix()
        for path in (repo / "pico").rglob("*.py")
        if path.is_file()
    }
    untracked_python = source_python - files
    if untracked_python:
        raise AssertionError(
            f"untracked package Python files: {sorted(untracked_python)}"
        )
    return files | PACKAGE_DATA_FILES


def _runtime_package_files(repo: Path, tracked_package_files: set[str]) -> set[str]:
    config = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    packages = set(config["tool"]["setuptools"]["packages"])
    runtime_files = set(PACKAGE_DATA_FILES)
    for name in tracked_package_files:
        path = PurePosixPath(name)
        package = path.parent.as_posix().replace("/", ".")
        if path.suffix == ".py" and package in packages:
            runtime_files.add(name)
    return runtime_files


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


def _smoke_env(home: Path, bin_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("PICO_") or name in {
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


def install_smoke(wheel: Path, *, offline: bool = False) -> None:
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
        env = _smoke_env(home, bin_dir)

        install_args = [str(python), "-m", "pip", "install", "--no-deps"]
        if offline:
            install_args.append("--no-index")
        install_args.append(str(wheel.resolve()))
        _run(*install_args, env=env)
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
        _run(
            str(python),
            "-c",
            "import importlib.util; "
            "forbidden=('pico.sandbox','pico.sandbox_macos','pico.sandbox_linux',"
            "'pico.sandbox_toolchain','pico.sandbox_lifecycle',"
            "'pico._sandbox_toolchain','pico.evaluation'); "
            "assert all(importlib.util.find_spec(name) is None for name in forbidden)",
            cwd=cwd,
            env=env,
        )
        _run(
            str(python),
            "-c",
            "from importlib.resources import files; "
            "root=files('pico._docker_sandbox'); "
            "root.joinpath('image-manifest.json').read_bytes(); "
            "root.joinpath('docker-config','config.json').read_bytes()",
            cwd=cwd,
            env=env,
        )
        resource_digest_code = (
            "import hashlib,json; from importlib.resources import files; "
            "root=files('pico._docker_sandbox'); "
            "names=('image-manifest.json','docker-config/config.json'); "
            "print(json.dumps({name:hashlib.sha256(root.joinpath(*name.split('/')).read_bytes()).hexdigest() "
            "for name in names},sort_keys=True))"
        )
        resources_before = _run(
            str(python),
            "-c",
            resource_digest_code,
            cwd=cwd,
            env=env,
        ).strip()
        status = json.loads(
            _run(
                str(pico),
                "--format",
                "json",
                "sandbox",
                "status",
                cwd=cwd,
                env=env,
            )
        )
        assert status["ok"] is True
        assert status["kind"] == "docker_sandbox_status"
        assert status["data"]["network_performed"] is False
        assert status["data"]["mutation_performed"] is False
        assert status["data"]["runtime_authorization"]["kind"] == "local"
        assert status["data"]["product_enablement"]["status"] == "blocked"
        assert status["data"]["capacity"]["staging_bytes"] == 0
        assert not (home / ".pico").exists()
        prepare = subprocess.run(
            (str(pico), "--format", "json", "sandbox", "prepare"),
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        prepare_payload = json.loads(prepare.stdout)
        assert prepare.returncode in {0, 3}, prepare.stderr
        if prepare.returncode == 0:
            prepared = prepare_payload["data"]
            assert prepared["network_performed"] is False
            assert prepared["mutation_performed"] is False
            assert prepared["runtime_authorization"]["kind"] == "local"
        else:
            assert prepare_payload["error"]["code"] in {
                "sandbox_image_not_released",
                "sandbox_image_missing",
                "sandbox_image_identity_mismatch",
                "docker_cli_unavailable",
                "docker_endpoint_untrusted",
                "docker_daemon_unavailable",
                "docker_server_unsupported",
                "docker_seccomp_unavailable",
                "docker_rootless_required",
            }
        assert not (home / ".pico").exists()
        resources_after = _run(
            str(python),
            "-c",
            resource_digest_code,
            cwd=cwd,
            env=env,
        ).strip()
        assert resources_after == resources_before
        _run(str(pico), "--help", cwd=cwd, env=env)
        _run(str(pico), "doctor", cwd=cwd, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--install-smoke", action="store_true")
    parser.add_argument("--offline-bundle-smoke", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
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
