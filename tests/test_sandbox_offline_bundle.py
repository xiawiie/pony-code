import hashlib
import io
from importlib import resources
import json
import os
from pathlib import Path
import tarfile

import pytest

import pico.sandbox_lifecycle as sandbox_lifecycle_module
from pico.sandbox_lifecycle import (
    ArchiveValidationError,
    bundle_tree_hash,
    export_bundle,
    import_bundle,
)
from pico.sandbox_toolchain import SandboxToolchain, ToolchainCorrupt

_SRT_INTEGRITY = (
    "sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77sMAI3Q9jP2h973h"
    "w5tg6dfEjyayc85rXixuAg=="
)


def _rewrite_manifest(archive, mutate):
    rewritten = archive.with_name("rewritten.tar")
    with tarfile.open(archive, "r") as source:
        members = source.getmembers()
        payloads = {
            member.name: source.extractfile(member).read()
            for member in members
            if member.isfile()
        }
    manifest = json.loads(payloads[".pico-bundle-manifest.json"])
    mutate(manifest)
    payloads[".pico-bundle-manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(rewritten, "w") as output:
        for member in members:
            data = payloads.get(member.name)
            if data is not None:
                member.size = len(data)
            output.addfile(member, io.BytesIO(data) if data is not None else None)
    return rewritten


def _self_declared_archive(tmp_path, toolchain):
    source = tmp_path / "source"
    node = source / "node" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho malicious\n", encoding="utf-8")
    node.chmod(0o700)
    srt = source / toolchain._entry["srt_entrypoint"]
    srt.parent.mkdir(parents=True)
    srt.write_text("malicious SRT", encoding="utf-8")
    marker = {
        "format_version": 1,
        "bundle_id": toolchain._entry["identity"],
        "tree": toolchain._tree(source),
        "package_lock_sha256": "",
        "srt_capability": "settings_schema_rejected",
    }
    (source / ".pico-toolchain.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    archive = tmp_path / "bundle.tar"
    export_bundle(
        source,
        archive,
        identity=toolchain._entry["identity"],
        platform="test",
        arch="x64",
    )
    return archive


def _trusted_archive(tmp_path):
    source = tmp_path / "trusted-source"
    node = source / "node" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"trusted node")
    node.chmod(0o700)
    (source / "node" / "LICENSE").write_text("Node license", encoding="utf-8")
    package_root = resources.files("pico._sandbox_toolchain")
    for name in ("package.json", "package-lock.json"):
        (source / name).write_bytes(package_root.joinpath(name).read_bytes())
    lock = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
    for package_path, package in lock["packages"].items():
        if package_path and package.get("license"):
            license_path = source / package_path / "LICENSE"
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_text(package["license"], encoding="utf-8")
            parts = Path(package_path).parts
            package_name = (
                "/".join(parts[1:3])
                if len(parts) == 3 and parts[1].startswith("@")
                else parts[-1]
            )
            (source / package_path / "package.json").write_text(
                json.dumps(
                    {
                        "name": package_name,
                        "version": package["version"],
                        "license": package["license"],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    srt_entrypoint = "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    srt = source / srt_entrypoint
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("trusted SRT", encoding="utf-8")
    tree = SandboxToolchain._tree(source)
    identity = "test-x64-node-24.18.0-srt-0.0.65"
    lock_hash = hashlib.sha256((source / "package-lock.json").read_bytes()).hexdigest()
    marker = {
        "format_version": 1,
        "bundle_id": identity,
        "tree": tree,
        "package_lock_sha256": lock_hash,
        "srt_capability": "settings_schema_rejected",
    }
    (source / ".pico-toolchain.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    toolchain = SandboxToolchain(
        tmp_path / "trusted-root",
        manifest={
            "schema_version": 1,
            "platforms": {
                "test-x64": {
                    "identity": identity,
                    "node_version": "24.18.0",
                    "srt_version": "0.0.65",
                    "srt_integrity": _SRT_INTEGRITY,
                    "srt_entrypoint": srt_entrypoint,
                    "offline_tree_sha256": bundle_tree_hash(tree),
                    "srt_capability": "settings_schema_rejected",
                }
            },
        },
        platform="test-x64",
        downloader=lambda _url: pytest.fail("offline import attempted network"),
        runner=lambda *_args, **_kwargs: pytest.fail("offline import executed archive code"),
    )
    archive = tmp_path / "trusted-bundle.tar"
    export_bundle(
        source,
        archive,
        identity=identity,
        platform="test",
        arch="x64",
        node_version="24.18.0",
        srt_version="0.0.65",
        package_lock_sha256=lock_hash,
        srt_capability="settings_schema_rejected",
    )
    return toolchain, source, archive


def _resign_and_export(toolchain, source, archive):
    tree = toolchain._tree(source)
    marker_path = source / ".pico-toolchain.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["tree"] = tree
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    toolchain._entry["offline_tree_sha256"] = bundle_tree_hash(tree)
    export_bundle(
        source,
        archive,
        identity=toolchain._entry["identity"],
        platform="test",
        arch="x64",
        node_version="24.18.0",
        srt_version="0.0.65",
        package_lock_sha256=marker["package_lock_sha256"],
        srt_capability="settings_schema_rejected",
    )
    return archive


def test_offline_round_trip_preserves_executable_mode(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    executable = source / "node"
    executable.write_bytes(b"node")
    executable.chmod(0o700)
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    destination = tmp_path / "destination"
    import_bundle(archive, destination, expected_platform="test", expected_arch="x")
    assert os.stat(destination / "node").st_mode & 0o111


def test_offline_import_rejects_archive_symlink(tmp_path):
    target = tmp_path / "target.tar"
    target.write_bytes(b"not an archive")
    link = tmp_path / "link.tar"
    link.symlink_to(target)
    with pytest.raises(ArchiveValidationError, match="unsafe"):
        import_bundle(link, tmp_path / "destination")


def test_offline_round_trip_preserves_internal_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.js").write_text("entry", encoding="utf-8")
    (source / "srt").symlink_to("entry.js")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    destination = tmp_path / "destination"
    import_bundle(archive, destination, expected_platform="test", expected_arch="x")
    assert (destination / "srt").is_symlink()
    assert (destination / "srt").read_text(encoding="utf-8") == "entry"


@pytest.mark.parametrize("version", (True, 1.0, "1", 2))
def test_offline_import_rejects_non_exact_manifest_version(tmp_path, version):
    source = tmp_path / "source"
    source.mkdir()
    (source / "node").write_bytes(b"node")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    rewritten = _rewrite_manifest(
        archive, lambda manifest: manifest.__setitem__("version", version)
    )

    with pytest.raises(ArchiveValidationError, match="manifest"):
        import_bundle(rewritten, tmp_path / "destination")


def test_offline_import_rejects_dangerous_manifest_mode(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "node").write_bytes(b"node")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")

    def dangerous_mode(manifest):
        manifest["files"][0]["mode"] = 0o777

    rewritten = _rewrite_manifest(archive, dangerous_mode)
    with pytest.raises(ArchiveValidationError, match="mode"):
        import_bundle(rewritten, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_offline_import_rejects_dangling_license_index(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "LICENSE").write_text("license", encoding="utf-8")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    rewritten = _rewrite_manifest(
        archive,
        lambda manifest: manifest.__setitem__("licenses", ["missing/LICENSE"]),
    )

    with pytest.raises(ArchiveValidationError, match="license"):
        import_bundle(rewritten, tmp_path / "destination")


def test_offline_import_rejects_symlinked_destination_parent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "node").write_bytes(b"node")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "bundles"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises((ArchiveValidationError, ValueError), match="symlink"):
        import_bundle(archive, parent / "id")
    assert not (outside / "id").exists()


def test_offline_import_rejects_self_declared_tree_without_trusted_provenance(
    tmp_path,
):
    identity = "test-x64-node-24.18.0-srt-0.0.65"
    toolchain = SandboxToolchain(
        tmp_path / "root",
        manifest={
            "schema_version": 1,
            "platforms": {
                "test-x64": {
                    "identity": identity,
                    "node_version": "24.18.0",
                    "srt_version": "0.0.65",
                    "srt_integrity": _SRT_INTEGRITY,
                    "srt_entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
                }
            },
        },
        platform="test-x64",
        downloader=lambda _url: pytest.fail("offline import attempted network"),
        runner=lambda *_args, **_kwargs: pytest.fail("offline import executed archive code"),
    )
    archive = _self_declared_archive(tmp_path, toolchain)

    with pytest.raises((ArchiveValidationError, ToolchainCorrupt)):
        import_bundle(
            archive,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert not toolchain.install_dir.exists()


def test_builtin_real_entry_without_release_provenance_fails_closed(tmp_path):
    toolchain = SandboxToolchain(
        tmp_path / "root",
        platform="darwin-arm64",
        downloader=lambda _url: pytest.fail("offline validation attempted network"),
        runner=lambda *_args, **_kwargs: pytest.fail("offline validation executed code"),
    )

    with pytest.raises(ToolchainCorrupt, match="offline .* not pinned"):
        toolchain.validate_offline_candidate(tmp_path / "unused", {})


def test_offline_import_publishes_only_trusted_complete_bundle(tmp_path):
    toolchain, _, archive = _trusted_archive(tmp_path)

    manifest = import_bundle(
        archive,
        toolchain.install_dir,
        expected_platform="test",
        expected_arch="x64",
        importer=toolchain.validate_offline_candidate,
    )

    assert manifest["identity"] == toolchain._entry["identity"]
    assert toolchain.validate()["status"] == "ready"
    assert (toolchain.install_dir / "node" / "bin" / "node").stat().st_mode & 0o777 == 0o500


@pytest.mark.parametrize("mutation", ("srt_version", "missing_dependency_metadata"))
def test_offline_import_rejects_self_consistent_installed_package_mismatch(
    tmp_path,
    mutation,
):
    toolchain, source, _ = _trusted_archive(tmp_path)
    if mutation == "srt_version":
        package_path = source / "node_modules/@anthropic-ai/sandbox-runtime/package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["version"] = "0.0.64"
        package_path.write_text(json.dumps(package, sort_keys=True), encoding="utf-8")
        expected_code = "srt_version_mismatch"
    else:
        (source / "node_modules/zod/package.json").unlink()
        expected_code = "toolchain_integrity_failed"
    archive = _resign_and_export(toolchain, source, tmp_path / f"{mutation}.tar")

    with pytest.raises(ToolchainCorrupt) as caught:
        import_bundle(
            archive,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )

    assert caught.value.code == expected_code
    assert not toolchain.install_dir.exists()


def test_ready_validation_requires_regular_license_for_every_locked_package(tmp_path):
    toolchain, _, archive = _trusted_archive(tmp_path)
    import_bundle(
        archive,
        toolchain.install_dir,
        importer=toolchain.validate_offline_candidate,
    )
    license_path = toolchain.install_dir / "node_modules/zod/LICENSE"
    license_path.unlink()
    marker_path = toolchain.install_dir / ".pico-toolchain.json"
    marker_path.chmod(0o600)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    tree = toolchain._tree(toolchain.install_dir)
    marker["tree"] = tree
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o400)
    toolchain._entry["offline_tree_sha256"] = bundle_tree_hash(tree)

    with pytest.raises(ToolchainCorrupt) as caught:
        toolchain.validate()

    assert caught.value.code == "toolchain_integrity_failed"


def test_archive_validation_error_has_fixed_code():
    assert ArchiveValidationError("invalid").code == "toolchain_archive_invalid"


def test_import_binds_archive_before_path_can_be_replaced(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("original", encoding="utf-8")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    replacement = tmp_path / "replacement.tar"
    replacement.write_bytes(b"not a tar archive")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and Path(path) == archive:
            swapped = True
            os.replace(replacement, archive)
        return descriptor

    monkeypatch.setattr(sandbox_lifecycle_module.os, "open", swapping_open)

    manifest = import_bundle(archive, tmp_path / "destination")

    assert swapped is True
    assert manifest["identity"] == "id"
    assert (tmp_path / "destination" / "file").read_text(encoding="utf-8") == "original"


def test_import_enumerates_members_before_materializing_unbounded_list(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "bundle.tar"
    archive.write_bytes(b"placeholder")
    monkeypatch.setattr(sandbox_lifecycle_module, "_MAX_IMPORT_FILES", 2)

    class FakeArchive:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for index in range(4):
                yield tarfile.TarInfo(f"file-{index}")

        def getmembers(self):
            raise AssertionError("getmembers must not run before the count limit")

    monkeypatch.setattr(
        sandbox_lifecycle_module.tarfile,
        "open",
        lambda *args, **kwargs: FakeArchive(),
    )

    with pytest.raises(ArchiveValidationError, match="too many"):
        import_bundle(archive, tmp_path / "destination")


def test_import_reads_tar_members_with_explicit_bounds(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"payload")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id", platform="test", arch="x")
    original_extractfile = tarfile.TarFile.extractfile
    read_sizes = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            assert size >= 0, "tar member reads must be bounded"
            read_sizes.append(size)
            return self.stream.read(size)

    def bounded_extractfile(self, member):
        stream = original_extractfile(self, member)
        return BoundedReader(stream) if stream is not None else None

    monkeypatch.setattr(tarfile.TarFile, "extractfile", bounded_extractfile)

    import_bundle(archive, tmp_path / "destination")

    assert read_sizes


def test_offline_import_rejects_symlink_used_as_member_parent(tmp_path):
    file_data = b"payload"
    link_data = b"symlink:real"
    files = [
        {
            "path": "link",
            "type": "symlink",
            "target": "real",
            "size": len(link_data),
            "mode": 0,
            "sha256": hashlib.sha256(link_data).hexdigest(),
        },
        {
            "path": "link/file",
            "type": "file",
            "size": len(file_data),
            "mode": 0o400,
            "sha256": hashlib.sha256(file_data).hexdigest(),
        },
    ]
    manifest = {
        "version": 1,
        "identity": "id",
        "platform": "test",
        "arch": "x",
        "node_version": "",
        "srt_version": "",
        "package_lock_sha256": "",
        "srt_capability": "",
        "tree_sha256": bundle_tree_hash(
            {entry["path"]: entry["sha256"] for entry in files}
        ),
        "total_size": sum(entry["size"] for entry in files),
        "licenses": [],
        "files": files,
    }
    archive = tmp_path / "parent-link.tar"
    with tarfile.open(archive, "w") as output:
        manifest_data = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(".pico-bundle-manifest.json")
        manifest_info.size = len(manifest_data)
        output.addfile(manifest_info, io.BytesIO(manifest_data))
        link_info = tarfile.TarInfo("link")
        link_info.type = tarfile.SYMTYPE
        link_info.linkname = "real"
        output.addfile(link_info)
        file_info = tarfile.TarInfo("link/file")
        file_info.size = len(file_data)
        output.addfile(file_info, io.BytesIO(file_data))

    with pytest.raises(ArchiveValidationError, match="member parent"):
        import_bundle(archive, tmp_path / "destination")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("identity", "wrong"),
        ("platform", "wrong"),
        ("arch", "arm64"),
        ("node_version", "0.0.0"),
        ("srt_version", "0.0.0"),
        ("package_lock_sha256", "0" * 64),
        ("srt_capability", "forged"),
        ("tree_sha256", "0" * 64),
    ),
)
def test_offline_import_rejects_wrong_trusted_metadata_before_publish(
    tmp_path, field, value
):
    toolchain, _, archive = _trusted_archive(tmp_path)
    rewritten = _rewrite_manifest(
        archive, lambda manifest: manifest.__setitem__(field, value)
    )

    with pytest.raises((ArchiveValidationError, ValueError, RuntimeError)):
        import_bundle(
            rewritten,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )

    assert not toolchain.install_dir.exists()


@pytest.mark.parametrize("license_case", ("empty", "missing_package"))
def test_offline_import_rejects_missing_required_license_before_publish(
    tmp_path, license_case
):
    toolchain, _, archive = _trusted_archive(tmp_path)

    def remove_license(manifest):
        if license_case == "empty":
            manifest["licenses"] = []
        else:
            manifest["licenses"] = [
                path
                for path in manifest["licenses"]
                if not path.startswith("node_modules/commander/")
            ]

    rewritten = _rewrite_manifest(archive, remove_license)
    with pytest.raises((ArchiveValidationError, ValueError, RuntimeError), match="license"):
        import_bundle(
            rewritten,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert not toolchain.install_dir.exists()


def test_offline_import_rejects_forged_tree_even_when_archive_is_self_consistent(
    tmp_path,
):
    toolchain, source, _ = _trusted_archive(tmp_path)
    node = source / "node" / "bin" / "node"
    node.write_bytes(b"forged node")
    marker_path = source / ".pico-toolchain.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["tree"] = toolchain._tree(source)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    archive = tmp_path / "forged-bundle.tar"
    export_bundle(
        source,
        archive,
        identity=toolchain._entry["identity"],
        platform="test",
        arch="x64",
        node_version="24.18.0",
        srt_version="0.0.65",
        package_lock_sha256=toolchain._bundled_lock_hash(),
        srt_capability="settings_schema_rejected",
    )

    with pytest.raises((ArchiveValidationError, ValueError, RuntimeError), match="tree|metadata"):
        import_bundle(
            archive,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert not toolchain.install_dir.exists()


def test_offline_import_refuses_to_replace_existing_ready_bundle(tmp_path):
    toolchain, _, archive = _trusted_archive(tmp_path)
    import_bundle(
        archive,
        toolchain.install_dir,
        importer=toolchain.validate_offline_candidate,
    )
    node = toolchain.install_dir / "node" / "bin" / "node"
    original = node.read_bytes()
    rewritten = _rewrite_manifest(
        archive, lambda manifest: manifest.__setitem__("identity", "wrong")
    )

    with pytest.raises(FileExistsError):
        import_bundle(
            rewritten,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert node.read_bytes() == original
    assert toolchain.validate()["status"] == "ready"


@pytest.mark.parametrize("missing", ("package.json", "package-lock.json"))
def test_offline_import_requires_exact_bundled_package_closure(tmp_path, missing):
    toolchain, source, _ = _trusted_archive(tmp_path)
    (source / missing).unlink()
    marker_path = source / ".pico-toolchain.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["tree"] = toolchain._tree(source)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    toolchain._entry["offline_tree_sha256"] = bundle_tree_hash(marker["tree"])
    archive = tmp_path / f"missing-{missing}.tar"
    export_bundle(
        source,
        archive,
        identity=toolchain._entry["identity"],
        platform="test",
        arch="x64",
        node_version="24.18.0",
        srt_version="0.0.65",
        package_lock_sha256=toolchain._bundled_lock_hash(),
        srt_capability="settings_schema_rejected",
    )

    with pytest.raises((ValueError, RuntimeError), match="package"):
        import_bundle(
            archive,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert not toolchain.install_dir.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (("format_version", True), ("srt_capability", "forged")),
)
def test_offline_import_rejects_forged_marker_evidence(
    tmp_path, field, value
):
    toolchain, source, _ = _trusted_archive(tmp_path)
    marker_path = source / ".pico-toolchain.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker[field] = value
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    archive = tmp_path / f"forged-{field}.tar"
    export_bundle(
        source,
        archive,
        identity=toolchain._entry["identity"],
        platform="test",
        arch="x64",
        node_version="24.18.0",
        srt_version="0.0.65",
        package_lock_sha256=toolchain._bundled_lock_hash(),
        srt_capability="settings_schema_rejected",
    )

    with pytest.raises((ValueError, RuntimeError), match="identity|capability"):
        import_bundle(
            archive,
            toolchain.install_dir,
            importer=toolchain.validate_offline_candidate,
        )
    assert not toolchain.install_dir.exists()
