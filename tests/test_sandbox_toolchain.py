import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tarfile
from types import SimpleNamespace

import pytest

import pico.sandbox as sandbox_module
import pico.sandbox_toolchain as sandbox_toolchain_module
from pico.sandbox_lifecycle import bundle_tree_hash
from pico.sandbox_toolchain import (
    SandboxToolchain,
    ToolchainNotReady,
    ToolchainCorrupt,
    UnsupportedPlatform,
    load_operator_mirror_config,
)

_SRT_INTEGRITY = (
    "sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77sMAI3Q9jP2h973h"
    "w5tg6dfEjyayc85rXixuAg=="
)


def archive_bytes(files=None, *, special=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in (files or {}).items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("bin/node") else 0o644
            archive.addfile(info, io.BytesIO(data))
        if special:
            archive.addfile(special)
    return stream.getvalue()


def manifest_for(payload, *, platform="test-platform"):
    return {
        "schema_version": 1,
        "platforms": {
            platform: {
                "url": "https://fixed.invalid/toolchain.tgz",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "identity": "toolchain-v1",
                "tree": {
                    "package.json": hashlib.sha256(b'{}').hexdigest(),
                    "bin/node": hashlib.sha256(b'node').hexdigest(),
                },
            }
        },
    }


def build(tmp_path, payload, **kwargs):
    calls = []
    runners = []

    def downloader(url):
        calls.append(url)
        return payload

    def runner(argv, **options):
        runners.append((argv, options))

    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest_for(payload),
        platform="test-platform",
        downloader=downloader,
        runner=runner,
        **kwargs,
    )
    return toolchain, calls, runners


def test_toolchain_lock_wait_defaults_to_plan_limit(tmp_path):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, _, _ = build(tmp_path, payload)

    assert toolchain.lock_timeout == 120


def test_unsupported_status_validate_install_repair_never_download(tmp_path):
    calls = []
    toolchain = SandboxToolchain(
        tmp_path / "root", manifest={"platforms": {}}, platform="missing",
        downloader=lambda url: calls.append(url), runner=lambda *a, **k: None,
    )
    assert toolchain.status()["status"] == "unsupported"
    for operation in (toolchain.validate, toolchain.install, toolchain.repair):
        with pytest.raises(UnsupportedPlatform):
            operation()
    assert calls == []


def test_rejected_bundled_candidate_never_creates_or_installs_toolchain(tmp_path):
    downloads = []
    root = tmp_path / "root"
    toolchain = SandboxToolchain(
        root,
        platform="darwin-arm64",
        downloader=lambda url: downloads.append(url),
    )

    expected = {
        "record_type": "sandbox_toolchain_status",
        "format_version": 1,
        "status": "not_ready",
        "platform": "darwin",
        "architecture": "arm64",
        "bundle_id": "darwin-arm64-node-24.18.0-srt-0.0.65",
        "node_version": "24.18.0",
        "srt_version": "0.0.65",
        "reason_code": "candidate_rejected",
    }
    assert toolchain.status() == expected
    assert toolchain.install() == expected
    assert toolchain.repair() == expected
    with pytest.raises(ToolchainNotReady) as caught:
        toolchain.validate()
    assert caught.value.code == "candidate_rejected"
    assert downloads == []
    assert not root.exists()


def test_f0_approval_does_not_enable_product_before_release(tmp_path):
    manifest = sandbox_toolchain_module._load_manifest()
    manifest["f0"] = {"status": "approved", "reason_code": ""}
    downloads = []
    root = tmp_path / "root"
    toolchain = SandboxToolchain(
        root,
        manifest=manifest,
        platform="darwin-arm64",
        downloader=lambda url: downloads.append(url),
    )

    assert toolchain.status()["reason_code"] == "sandbox_not_released"
    assert toolchain.install()["reason_code"] == "sandbox_not_released"
    assert toolchain.repair()["reason_code"] == "sandbox_not_released"
    with pytest.raises(ToolchainNotReady) as caught:
        toolchain.validate()
    assert caught.value.code == "sandbox_not_released"
    assert downloads == []
    assert not root.exists()

    manifest["product"] = {"status": "enabled", "reason_code": ""}
    enabled = SandboxToolchain(
        tmp_path / "enabled-root",
        manifest=manifest,
        platform="darwin-arm64",
        create_root=False,
    )
    assert enabled.status()["status"] == "absent"


def test_rejected_candidate_precedes_unsupported_platform(tmp_path):
    calls = []
    root = tmp_path / "root"
    toolchain = SandboxToolchain(
        root,
        manifest={
            "platforms": {},
            "f0": {"status": "rejected", "reason_code": "candidate_rejected"},
        },
        platform="windows-x64",
        downloader=lambda url: calls.append(url),
    )

    assert toolchain.status()["reason_code"] == "candidate_rejected"
    assert toolchain.install()["reason_code"] == "candidate_rejected"
    assert toolchain.repair()["reason_code"] == "candidate_rejected"
    with pytest.raises(ToolchainNotReady) as caught:
        toolchain.validate()
    assert caught.value.code == "candidate_rejected"
    assert calls == []
    assert not root.exists()


def test_supported_system_with_unknown_architecture_uses_fixed_reason_code(tmp_path):
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest={"platforms": {"linux-x64": {"identity": "unused"}}},
        platform="linux-riscv64",
    )

    assert toolchain.status()["reason_code"] == "unsupported_architecture"
    with pytest.raises(UnsupportedPlatform) as caught:
        toolchain.validate()
    assert caught.value.code == "unsupported_architecture"


def test_install_verifies_prebuilt_archive_and_validates_tree(tmp_path):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, calls, runners = build(tmp_path, payload)
    result = toolchain.install()
    assert result["status"] == "ready"
    assert calls == ["https://fixed.invalid/toolchain.tgz"]
    assert runners == []
    assert toolchain.validate()["bundle_id"] == "toolchain-v1"


def test_real_manifest_uses_managed_npm_and_bundled_lock(tmp_path, monkeypatch):
    payload = archive_bytes(
        {
            "node-v24.18.0-test/bin/node": "node",
            "node-v24.18.0-test/LICENSE": "node license",
            "node-v24.18.0-test/lib/node_modules/npm/bin/npm-cli.js": "npm",
        }
    )
    package_root = sandbox_toolchain_module.resources.files("pico._sandbox_toolchain")
    lock = json.loads(package_root.joinpath("package-lock.json").read_text(encoding="utf-8"))
    srt_entrypoint = "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    expected_tree = {
        "node/bin/node": hashlib.sha256(b"node").hexdigest(),
        "node/LICENSE": hashlib.sha256(b"node license").hexdigest(),
        "node/lib/node_modules/npm/bin/npm-cli.js": hashlib.sha256(b"npm").hexdigest(),
        "package.json": hashlib.sha256(package_root.joinpath("package.json").read_bytes()).hexdigest(),
        "package-lock.json": hashlib.sha256(package_root.joinpath("package-lock.json").read_bytes()).hexdigest(),
        srt_entrypoint: hashlib.sha256(b"srt").hexdigest(),
    }
    for package_path, package in lock["packages"].items():
        if not package_path:
            continue
        metadata = json.dumps(
            {
                "name": SandboxToolchain._package_name(package_path),
                "version": package["version"],
                "license": package["license"],
            },
            sort_keys=True,
        ).encode()
        expected_tree[f"{package_path}/package.json"] = hashlib.sha256(metadata).hexdigest()
        expected_tree[f"{package_path}/LICENSE"] = hashlib.sha256(b"license").hexdigest()
    manifest = {
        "schema_version": 1,
        "platforms": {
            "test-platform": {
                "url": "https://fixed.invalid/node.tgz",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "identity": "test-platform-node-24.18.0-srt-0.0.65",
                "node_version": "24.18.0",
                "srt_version": "0.0.65",
                "srt_integrity": lock["packages"][
                    "node_modules/@anthropic-ai/sandbox-runtime"
                ]["integrity"],
                "srt_entrypoint": srt_entrypoint,
                "offline_tree_sha256": bundle_tree_hash(expected_tree),
                "srt_capability": "settings_schema_rejected",
            }
        },
    }
    calls = []

    def runner(argv, **options):
        cwd = options["cwd"]
        calls.append((argv, options))
        assert Path(argv[0]) == cwd / "node/bin/node"
        if argv[1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout="v24.18.0\n")
        assert Path(argv[1]) == cwd / "node/lib/node_modules/npm/bin/npm-cli.js"
        assert argv[2:] == [
            "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund",
        ]
        assert json.loads((cwd / "package.json").read_text())["private"] is True
        assert (cwd / "package-lock.json").is_file()
        env = options["env"]
        assert env["npm_config_userconfig"] != env["npm_config_globalconfig"]
        assert Path(env["npm_config_userconfig"]).read_text() == ""
        assert Path(env["npm_config_globalconfig"]).read_text() == ""
        assert env["npm_config_registry"] == "https://mirror.example/npm/"
        assert env["npm_config_replace_registry_host"] == "always"
        for package_path, package in lock["packages"].items():
            if not package_path:
                continue
            package_dir = cwd / package_path
            package_dir.mkdir(parents=True, exist_ok=True)
            (package_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": SandboxToolchain._package_name(package_path),
                        "version": package["version"],
                        "license": package["license"],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (package_dir / "LICENSE").write_text("license", encoding="utf-8")
        entry = cwd / manifest["platforms"]["test-platform"]["srt_entrypoint"]
        entry.parent.mkdir(parents=True)
        entry.write_text("srt")
        return None

    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest,
        platform="test-platform",
        downloader=lambda url: payload,
        runner=runner,
        mirror={
            "node_base_url": "https://mirror.example/node",
            "npm_registry_url": "https://mirror.example/npm",
        },
    )
    validations = []
    validate_directory = toolchain._validate_directory

    def track_validation(directory, **kwargs):
        validations.append((Path(directory), kwargs))
        return validate_directory(directory, **kwargs)

    monkeypatch.setattr(toolchain, "_validate_directory", track_validation)
    assert toolchain.install()["status"] == "ready"
    assert validations[0][0].parent == toolchain.root / "staging"
    assert validations[0][1] == {
        "trusted_tree_sha256": manifest["platforms"]["test-platform"][
            "offline_tree_sha256"
        ],
        "expected_capability": "settings_schema_rejected",
    }
    assert validations[-1][0] == toolchain.install_dir
    assert len(calls) == 2
    identity = toolchain.identity()
    assert identity.node_path == toolchain.install_dir / "node/bin/node"
    assert identity.srt_entry_path.is_file()
    assert identity.bundle_manifest_hash
    assert identity.file_identities
    identity.srt_entry_path.chmod(0o600)
    identity.srt_entry_path.write_text("replaced")
    with pytest.raises(ValueError, match="identity changed"):
        identity.verify()


def test_managed_node_version_mismatch_stops_before_npm(tmp_path):
    payload = archive_bytes(
        {
            "node-v24.18.0-test/bin/node": "node",
            "node-v24.18.0-test/lib/node_modules/npm/bin/npm-cli.js": "npm",
        }
    )
    calls = []

    def runner(argv, **_options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="v23.0.0\n")

    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest={
            "platforms": {
                "test-platform": {
                    "url": "https://fixed.invalid/node.tgz",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "identity": "test-node-srt",
                    "node_version": "24.18.0",
                    "srt_version": "0.0.65",
                    "srt_integrity": _SRT_INTEGRITY,
                    "srt_entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
                    "archive_root": "node-v24.18.0-test",
                    "offline_tree_sha256": "0" * 64,
                    "srt_capability": "settings_schema_rejected",
                }
            }
        },
        platform="test-platform",
        downloader=lambda _url: payload,
        runner=runner,
    )

    with pytest.raises(ToolchainCorrupt, match="Node version mismatch"):
        toolchain.install()

    assert len(calls) == 1
    assert calls[0][1:] == ["--version"]
    assert not toolchain.install_dir.exists()


@pytest.mark.parametrize(
    ("platform_id", "machine"),
    (("linux-x64", 183), ("linux-arm64", 62)),
)
def test_managed_node_elf_architecture_mismatch_is_rejected(
    tmp_path, platform_id, machine
):
    node = tmp_path / "node"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, machine)
    node.write_bytes(header)

    with pytest.raises(ToolchainCorrupt, match="architecture"):
        sandbox_toolchain_module._verify_binary_architecture(node, platform_id)


def test_bundled_manifest_pins_exact_archive_root():
    manifest = sandbox_toolchain_module._load_manifest()

    assert manifest["platforms"]["linux-x64"]["archive_root"] == (
        "node-v24.18.0-linux-x64"
    )


@pytest.mark.parametrize(
    ("filename", "mutate"),
    (
        (
            "package.json",
            lambda raw: raw.replace(
                b'"name": "pico-sandbox-toolchain"',
                b'"name": "pico-sandbox-toolchain", "name": "duplicate"',
            ),
        ),
        (
            "package-lock.json",
            lambda raw: raw.replace(
                b'"name": "pico-sandbox-toolchain"',
                b'"name": "pico-sandbox-toolchain", "name": "duplicate"',
                1,
            ),
        ),
        (
            "package.json",
            lambda raw: json.dumps(
                {**json.loads(raw), "scripts": {"install": "forbidden"}}
            ).encode(),
        ),
    ),
)
def test_bundled_package_metadata_rejects_duplicate_or_unknown_schema(
    monkeypatch,
    filename,
    mutate,
):
    entry = sandbox_toolchain_module._load_manifest()["platforms"]["darwin-arm64"]
    original = SandboxToolchain._bundled_package_bytes

    def package_bytes(name):
        raw = original(name)
        return mutate(raw) if name == filename else raw

    monkeypatch.setattr(
        SandboxToolchain,
        "_bundled_package_bytes",
        staticmethod(package_bytes),
    )

    with pytest.raises(ToolchainCorrupt) as caught:
        SandboxToolchain._validate_bundled_package_metadata(entry)

    assert caught.value.code == "toolchain_integrity_failed"


@pytest.mark.parametrize("mutation", ("missing_dependency", "http_resolved", "missing_license"))
def test_bundled_lock_requires_exact_closed_licensed_https_packages(
    monkeypatch,
    mutation,
):
    entry = sandbox_toolchain_module._load_manifest()["platforms"]["darwin-arm64"]
    original = SandboxToolchain._bundled_package_bytes
    lock = json.loads(original("package-lock.json"))
    if mutation == "missing_dependency":
        del lock["packages"]["node_modules/zod"]
    elif mutation == "http_resolved":
        lock["packages"]["node_modules/zod"]["resolved"] = "http://registry.invalid/zod.tgz"
    else:
        del lock["packages"]["node_modules/zod"]["license"]

    monkeypatch.setattr(
        SandboxToolchain,
        "_bundled_package_bytes",
        staticmethod(
            lambda name: json.dumps(lock).encode()
            if name == "package-lock.json"
            else original(name)
        ),
    )

    with pytest.raises(ToolchainCorrupt) as caught:
        SandboxToolchain._validate_bundled_package_metadata(entry)

    assert caught.value.code == "toolchain_integrity_failed"


def test_marker_read_rejects_path_swap_after_descriptor_open(tmp_path, monkeypatch):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, _, _ = build(tmp_path, payload)
    toolchain.install()
    marker = toolchain.install_dir / ".pico-toolchain.json"
    replacement = tmp_path / ".replacement-marker.json"
    replacement.write_bytes(marker.read_bytes())
    replacement.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and os.fspath(path) == ".pico-toolchain.json":
            swapped = True
            os.replace(replacement, marker)
        return descriptor

    monkeypatch.setattr(sandbox_toolchain_module.os, "open", swapping_open)

    with pytest.raises(ToolchainCorrupt, match="changed|unsafe"):
        toolchain.validate()

    assert swapped is True


def test_runtime_identity_rejects_marker_swap_after_descriptor_open(
    tmp_path, monkeypatch
):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, _, _ = build(tmp_path, payload)
    toolchain.install()
    identity = toolchain.identity()
    marker = toolchain.install_dir / ".pico-toolchain.json"
    replacement = tmp_path / ".runtime-replacement-marker.json"
    replacement.write_bytes(marker.read_bytes())
    replacement.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and os.fspath(path) == ".pico-toolchain.json":
            swapped = True
            os.replace(replacement, marker)
        return descriptor

    monkeypatch.setattr(sandbox_module.os, "open", swapping_open)

    with pytest.raises(ValueError, match="bundle manifest changed"):
        identity.verify()

    assert swapped is True


def test_managed_bundle_requires_release_pinned_tree_before_download(tmp_path):
    calls = []
    manifest = {
        "schema_version": 1,
        "platforms": {
            "test-platform": {
                "url": "https://fixed.invalid/node.tgz",
                "sha256": "0" * 64,
                "identity": "test-platform-node-24.18.0-srt-0.0.65",
                "node_version": "24.18.0",
                "srt_version": "0.0.65",
                "srt_integrity": _SRT_INTEGRITY,
                "srt_entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
            }
        },
    }
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest,
        platform="test-platform",
        downloader=lambda url: calls.append(url),
    )

    with pytest.raises(ToolchainCorrupt, match="not pinned"):
        toolchain.install()

    assert calls == []


def test_validate_rejects_self_consistent_managed_bundle_without_release_pin(tmp_path):
    entrypoint = "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    manifest = {
        "schema_version": 1,
        "platforms": {
            "test-platform": {
                "identity": "test-platform-node-24.18.0-srt-0.0.65",
                "node_version": "24.18.0",
                "srt_version": "0.0.65",
                "srt_integrity": _SRT_INTEGRITY,
                "srt_entrypoint": entrypoint,
            }
        },
    }
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest,
        platform="test-platform",
    )
    node = toolchain.install_dir / "node" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho forged\n", encoding="utf-8")
    node.chmod(0o700)
    srt = toolchain.install_dir / entrypoint
    srt.parent.mkdir(parents=True)
    srt.write_text("forged SRT", encoding="utf-8")
    package_root = sandbox_toolchain_module.resources.files("pico._sandbox_toolchain")
    for name in ("package.json", "package-lock.json"):
        (toolchain.install_dir / name).write_bytes(package_root.joinpath(name).read_bytes())
    tree = toolchain._tree(toolchain.install_dir)
    marker = {
        "format_version": 1,
        "bundle_id": toolchain._entry["identity"],
        "tree": tree,
        "package_lock_sha256": toolchain._bundled_lock_hash(),
        "srt_capability": "settings_schema_rejected",
    }
    (toolchain.install_dir / ".pico-toolchain.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )

    with pytest.raises(ToolchainCorrupt, match="not pinned"):
        toolchain.validate()


def test_bad_size_never_publishes_install(tmp_path):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, _, _ = build(tmp_path, payload)
    toolchain.downloader = lambda url: payload + b"tampered"
    with pytest.raises(ValueError, match="size"):
        toolchain.install()
    assert not toolchain.install_dir.exists()


@pytest.mark.parametrize("member", ["../escape", "/absolute"])
def test_safe_unpack_rejects_traversal(tmp_path, member):
    payload = archive_bytes(special=tarfile.TarInfo(member))
    toolchain, _, runners = build(tmp_path, payload)
    toolchain._entry["tree"] = {}
    with pytest.raises(ValueError, match="unsafe tar member"):
        toolchain.install()
    assert runners == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "device"])
def test_safe_unpack_rejects_links_and_special_files(tmp_path, kind):
    info = tarfile.TarInfo("bad")
    if kind == "symlink":
        info.type, info.linkname = tarfile.SYMTYPE, "target"
    elif kind == "hardlink":
        info.type, info.linkname = tarfile.LNKTYPE, "target"
    elif kind == "fifo":
        info.type = tarfile.FIFOTYPE
    else:
        info.type = tarfile.CHRTYPE
    payload = archive_bytes(special=info)
    toolchain, _, runners = build(tmp_path, payload)
    toolchain._entry["tree"] = {}
    with pytest.raises(ValueError, match="unsafe tar member"):
        toolchain.install()
    assert runners == []


def test_corrupt_install_refuses_install_and_repair_replaces_it(tmp_path, monkeypatch):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain, _, _ = build(tmp_path, payload)
    toolchain.install()
    node = toolchain.install_dir / "bin/node"
    node.chmod(0o700)
    node.write_text("corrupt")
    status = toolchain.status()
    assert status["status"] == "corrupt"
    assert set(status) == {
        "record_type",
        "format_version",
        "status",
        "platform",
        "architecture",
        "bundle_id",
        "node_version",
        "srt_version",
        "reason_code",
    }
    with pytest.raises(ToolchainCorrupt):
        toolchain.install()
    fsyncs = []
    monkeypatch.setattr(toolchain, "_fsync_dir", lambda path: fsyncs.append(Path(path)))
    assert toolchain.repair()["status"] == "ready"
    assert list((toolchain.root / "quarantine").iterdir()) == []
    assert fsyncs.count(toolchain.install_dir.parent) >= 2
    assert fsyncs.count(toolchain.root / "quarantine") >= 2


def test_root_must_be_owner_only(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    payload = archive_bytes({})
    with pytest.raises(PermissionError, match="owner-only"):
        SandboxToolchain(root, manifest=manifest_for(payload), platform="test-platform")


def _write_mirror_config(path, **overrides):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {
        "node_base_url": "https://mirror.example/node",
        "npm_registry_url": "https://mirror.example/npm",
        **overrides,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_operator_mirror_only_reads_user_config_or_explicit_config_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_mirror_config(workspace / ".pico" / "sandbox-mirror.json")
    monkeypatch.chdir(workspace)

    assert load_operator_mirror_config(home=home, env={}) is None

    default = _write_mirror_config(home / ".pico" / "sandbox-mirror.json")
    loaded = load_operator_mirror_config(home=home, env={})
    assert loaded == {
        "node_base_url": "https://mirror.example/node/",
        "npm_registry_url": "https://mirror.example/npm/",
        "source": "user_config",
    }
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest_for(payload),
        platform="test-platform",
        downloader=lambda _url: payload,
        mirror=loaded,
    )
    assert toolchain._artifact_url(toolchain._entry) == "https://mirror.example/node/toolchain.tgz"

    alternate = _write_mirror_config(tmp_path / "operator" / "mirror.json")
    assert load_operator_mirror_config(
        home=home,
        env={"PICO_SANDBOX_MIRROR_CONFIG": str(alternate)},
    )["source"] == "environment"
    assert default.is_file()


def test_operator_mirror_rejects_relative_unsafe_or_incomplete_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = _write_mirror_config(home / ".pico" / "sandbox-mirror.json")
    config.chmod(0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        load_operator_mirror_config(home=home, env={})

    config.chmod(0o600)
    config.write_text(json.dumps({"node_base_url": "https://mirror.example/node"}))
    with pytest.raises(ValueError, match="schema"):
        load_operator_mirror_config(home=home, env={})

    with pytest.raises(ValueError, match="absolute"):
        load_operator_mirror_config(
            home=home,
            env={"PICO_SANDBOX_MIRROR_CONFIG": "relative.json"},
        )


def test_explicit_mirror_failure_never_falls_back_and_pinned_hash_still_applies(tmp_path):
    payload = archive_bytes({"package.json": "{}", "bin/node": "node"})
    calls = []

    def downloader(url):
        calls.append(url)
        return payload + b"tampered"

    manifest = manifest_for(payload)
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest=manifest,
        platform="test-platform",
        downloader=downloader,
        mirror={
            "node_base_url": "https://mirror.example/node",
            "npm_registry_url": "https://mirror.example/npm",
        },
    )

    with pytest.raises(ValueError, match="size|hash"):
        toolchain.install()
    assert calls == ["https://mirror.example/node/toolchain.tgz"]
    assert toolchain._entry["url"] == "https://fixed.invalid/toolchain.tgz"
    assert not toolchain.install_dir.exists()


class _DownloadResponse:
    def __init__(self, url, chunks, content_length=None):
        self.url = url
        self.chunks = iter(chunks)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _size):
        return next(self.chunks, b"")


def test_download_rejects_redirect_outside_allowlist(monkeypatch):
    monkeypatch.setattr(
        sandbox_toolchain_module,
        "urlopen",
        lambda _url, timeout: _DownloadResponse("https://example.invalid/node.tgz", []),
    )
    with pytest.raises(ValueError, match="redirect"):
        sandbox_toolchain_module._download("https://nodejs.org/node.tgz")


def test_download_enforces_stream_limit_without_content_length(monkeypatch):
    monkeypatch.setattr(sandbox_toolchain_module, "_MAX_ARCHIVE_BYTES", 3)
    monkeypatch.setattr(
        sandbox_toolchain_module,
        "urlopen",
        lambda _url, timeout: _DownloadResponse("https://nodejs.org/node.tgz", [b"abcd"]),
    )
    with pytest.raises(ValueError, match="size limit"):
        sandbox_toolchain_module._download("https://nodejs.org/node.tgz")


def test_mirror_download_rejects_redirect_to_official_origin(monkeypatch):
    monkeypatch.setattr(
        sandbox_toolchain_module,
        "urlopen",
        lambda _url, timeout: _DownloadResponse("https://nodejs.org/node.tgz", []),
    )
    with pytest.raises(ValueError, match="redirect"):
        sandbox_toolchain_module._download(
            "https://mirror.example/node.tgz",
            allowed_hosts={"mirror.example"},
        )
