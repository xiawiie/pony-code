import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

import pico.sandbox_toolchain as sandbox_toolchain_module
from pico.sandbox_toolchain import SandboxToolchain

_SRT_INTEGRITY = (
    "sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77sMAI3Q9jP2h973h"
    "w5tg6dfEjyayc85rXixuAg=="
)


def _archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("/node") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _prebuilt(tmp_path, payload, *, downloader=None):
    return SandboxToolchain(
        tmp_path / "root",
        manifest={
            "platforms": {
                "test": {
                    "url": "https://fixed.invalid/toolchain.tgz",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                    "identity": "test-v1",
                    "tree": {
                        "bin/node": hashlib.sha256(b"node").hexdigest(),
                        "package.json": hashlib.sha256(b"{}").hexdigest(),
                    },
                }
            }
        },
        platform="test",
        downloader=downloader or (lambda _url: payload),
    )


def test_insufficient_disk_fails_before_download_or_publish(tmp_path, monkeypatch):
    payload = _archive({"bin/node": "node", "package.json": "{}"})
    downloads = []
    toolchain = _prebuilt(tmp_path, payload, downloader=lambda url: downloads.append(url))
    monkeypatch.setattr(
        sandbox_toolchain_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(OSError, match="disk space"):
        toolchain.install()
    assert downloads == []
    assert not toolchain.install_dir.exists()


def test_install_runner_failure_cleans_staging_and_never_publishes(tmp_path):
    payload = _archive(
        {
            "node-v24-test/bin/node": "node",
            "node-v24-test/lib/node_modules/npm/bin/npm-cli.js": "npm",
        }
    )
    entry = {
        "url": "https://fixed.invalid/node.tgz",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "identity": "test-node-srt",
        "node_version": "24.18.0",
        "srt_version": "0.0.65",
        "srt_integrity": _SRT_INTEGRITY,
        "srt_entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
        "offline_tree_sha256": "0" * 64,
        "srt_capability": "settings_schema_rejected",
    }
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout="v24.18.0\n")
        raise RuntimeError("npm failed")

    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest={"platforms": {"test": entry}},
        platform="test",
        downloader=lambda _url: payload,
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="npm failed"):
        toolchain.install()
    assert not toolchain.install_dir.exists()
    assert list((toolchain.root / "staging").iterdir()) == []
    assert len(calls) == 2


def test_staging_validation_failure_never_publishes(tmp_path, monkeypatch):
    payload = _archive({"bin/node": "node", "package.json": "{}"})
    toolchain = _prebuilt(tmp_path, payload)
    validate_directory = toolchain._validate_directory

    def reject_staging(directory, **kwargs):
        if Path(directory).parent == toolchain.root / "staging":
            raise RuntimeError("staging validation failed")
        return validate_directory(directory, **kwargs)

    monkeypatch.setattr(toolchain, "_validate_directory", reject_staging)

    with pytest.raises(RuntimeError, match="staging validation failed"):
        toolchain.install()
    assert not toolchain.install_dir.exists()
    assert list((toolchain.root / "staging").iterdir()) == []


def test_failed_repair_restores_corrupt_bundle_as_authoritative(tmp_path, monkeypatch):
    payload = _archive({"bin/node": "node", "package.json": "{}"})
    toolchain = _prebuilt(tmp_path, payload)
    toolchain.install()
    (toolchain.install_dir / "bin/node").chmod(0o700)
    (toolchain.install_dir / "bin/node").write_text("corrupt", encoding="utf-8")
    toolchain.downloader = lambda _url: (_ for _ in ()).throw(OSError("offline"))
    fsyncs = []
    monkeypatch.setattr(toolchain, "_fsync_dir", lambda path: fsyncs.append(Path(path)))

    with pytest.raises(OSError, match="offline"):
        toolchain.repair()
    assert toolchain.status()["status"] == "corrupt"
    assert (toolchain.install_dir / "bin/node").read_text(encoding="utf-8") == "corrupt"
    assert list((toolchain.root / "quarantine").iterdir()) == []
    assert fsyncs.count(toolchain.install_dir.parent) == 2
    assert fsyncs.count(toolchain.root / "quarantine") == 2


def test_failed_post_publish_repair_removes_new_bundle_and_restores_old(
    tmp_path, monkeypatch
):
    payload = _archive({"bin/node": "node", "package.json": "{}"})
    toolchain = _prebuilt(tmp_path, payload)
    toolchain.install()
    node = toolchain.install_dir / "bin/node"
    node.chmod(0o700)
    node.write_text("corrupt", encoding="utf-8")
    validate_directory = toolchain._validate_directory

    def fail_after_publish(directory, **kwargs):
        result = validate_directory(directory, **kwargs)
        if Path(directory) == toolchain.install_dir:
            raise RuntimeError("post-publish validation failed")
        return result

    monkeypatch.setattr(toolchain, "_validate_directory", fail_after_publish)

    with pytest.raises(RuntimeError, match="post-publish validation failed"):
        toolchain.repair()
    monkeypatch.setattr(toolchain, "_validate_directory", validate_directory)
    assert toolchain.status()["status"] == "corrupt"
    assert node.read_text(encoding="utf-8") == "corrupt"
    assert list((toolchain.root / "quarantine").iterdir()) == []
