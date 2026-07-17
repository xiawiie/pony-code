#!/usr/bin/env python3
"""Build Pony's locked Linux/arm64 development sandbox image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "docker" / "sandbox" / "image-inputs.lock.json"
DOCKERFILE = ROOT / "docker" / "sandbox" / "Dockerfile"
DEFAULT_CACHE = Path.home() / ".cache" / "pony" / "sandbox-build"
DEFAULT_TAG = "pony-sandbox:local"
ALLOWED_HOSTS = {
    "files.pythonhosted.org",
    "github.com",
    "release-assets.githubusercontent.com",
    "snapshot.debian.org",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BuildError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BuildError(f"not a regular build input: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid image lock: {path}") from exc
    if (
        not isinstance(lock, dict)
        or lock.get("format_version") != 1
        or lock.get("platform") != "linux/arm64"
    ):
        raise BuildError("unsupported image lock")
    base = lock.get("base_image")
    if not isinstance(base, dict) or not re.fullmatch(
        r"python@sha256:[0-9a-f]{64}", str(base.get("reference", ""))
    ):
        raise BuildError("invalid locked base image")
    assets = [
        lock.get("uv"),
        *(lock.get("python_wheels") or []),
        *(lock.get("debian_packages") or []),
    ]
    if not assets:
        raise BuildError("image lock has no assets")
    names = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise BuildError("invalid locked asset")
        name = asset.get("filename")
        digest = asset.get("sha256")
        size = asset.get("size")
        parsed = urllib.parse.urlparse(str(asset.get("url", "")))
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in names
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
            or type(size) is not int
            or size <= 0
            or parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
        ):
            raise BuildError(f"invalid locked asset: {name!r}")
        names.add(name)
    return lock


def _verified(path: Path, asset: dict) -> bool:
    try:
        return path.stat().st_size == asset["size"] and _digest(path) == asset["sha256"]
    except (OSError, BuildError):
        return False


def _download(asset: dict, cache: Path, *, offline: bool) -> Path:
    target = cache / asset["filename"]
    if _verified(target, asset):
        return target
    if offline:
        raise BuildError(f"locked asset is not cached: {asset['filename']}")
    target.unlink(missing_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        request = urllib.request.Request(
            asset["url"], headers={"User-Agent": "pony-sandbox-builder/1"}
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                raise BuildError("asset redirected to an untrusted host")
            digest = hashlib.sha256()
            total = 0
            with temporary_path.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > asset["size"]:
                        raise BuildError(f"asset exceeds locked size: {asset['filename']}")
                    digest.update(chunk)
                    stream.write(chunk)
        if total != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise BuildError(f"asset checksum mismatch: {asset['filename']}")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
        return target
    finally:
        temporary_path.unlink(missing_ok=True)


def _copy(source: Path, destination: Path, expected: str) -> None:
    if _digest(source) != expected:
        raise BuildError(f"build input checksum mismatch: {source}")
    shutil.copyfile(source, destination, follow_symlinks=False)
    if _digest(destination) != expected:
        raise BuildError(f"copied build input changed: {destination}")


def _context(path: Path, lock: dict, cache: Path) -> None:
    inputs = path / "inputs"
    for group in ("debs", "wheels", "uv"):
        (inputs / group).mkdir(parents=True)
    shutil.copyfile(DOCKERFILE, path / "Dockerfile", follow_symlinks=False)
    shutil.copyfile(LOCK_PATH, path / "image-inputs.lock.json", follow_symlinks=False)
    build_inputs = lock.get("build_inputs")
    if not isinstance(build_inputs, dict):
        raise BuildError("image lock has no build input digests")
    _copy(ROOT / "pyproject.toml", path / "pyproject.toml", build_inputs["pyproject_sha256"])
    _copy(ROOT / "uv.lock", path / "uv.lock", build_inputs["uv_lock_sha256"])
    checksums = []
    groups = (
        ("uv", [lock["uv"]]),
        ("wheels", lock["python_wheels"]),
        ("debs", lock["debian_packages"]),
    )
    for group, assets in groups:
        for asset in assets:
            relative = Path(group) / asset["filename"]
            _copy(cache / asset["filename"], inputs / relative, asset["sha256"])
            checksums.append(f"{asset['sha256']}  {relative.as_posix()}\n")
    (inputs / "SHA256SUMS").write_text("".join(checksums), encoding="ascii")


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise BuildError(f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise BuildError(f"command failed ({exc.returncode}): {detail[-4000:]}") from exc


def build(*, docker: str, cache: Path, tag: str, offline: bool) -> dict:
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = _load_lock(LOCK_PATH)
    assets = [lock["uv"], *lock["python_wheels"], *lock["debian_packages"]]
    for asset in assets:
        _download(asset, cache, offline=offline)
    base = lock["base_image"]["reference"]
    if not offline:
        _run([docker, "pull", "--platform=linux/arm64", base])
    with tempfile.TemporaryDirectory(prefix="pony-sandbox-build-") as raw:
        context = Path(raw)
        _context(context, lock, cache)
        _run(
            [
                docker,
                "buildx",
                "build",
                "--platform=linux/arm64",
                "--pull=false",
                "--network=none",
                "--provenance=false",
                f"--build-arg=BASE_REFERENCE={base}",
                f"--tag={tag}",
                "--load",
                str(context),
            ]
        )
    inspected = json.loads(_run([docker, "image", "inspect", tag]).stdout)
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise BuildError("docker returned an invalid image inspection")
    descriptor = inspected[0].get("Descriptor")
    if not isinstance(descriptor, dict):
        raise BuildError("built image has no stable descriptor")
    image_digest = descriptor.get("digest")
    annotations = descriptor.get("annotations")
    image_id = annotations.get("config.digest") if isinstance(annotations, dict) else None
    if (
        not isinstance(image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
        or not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or inspected[0].get("Id") not in {image_digest, image_id}
    ):
        raise BuildError("built image has no stable identity")
    return {
        "record_type": "pony_sandbox_local_build",
        "format_version": 1,
        "status": "built",
        "platform": "linux/arm64",
        "tag": tag,
        "image_digest": image_digest,
        "image_id": image_id,
        "base_reference": base,
        "dockerfile_sha256": _digest(DOCKERFILE),
        "lock_sha256": _digest(LOCK_PATH),
        "asset_count": len(assets),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--offline", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build(
            docker=args.docker,
            cache=args.cache.expanduser(),
            tag=args.tag,
            offline=args.offline,
        )
    except (BuildError, KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
