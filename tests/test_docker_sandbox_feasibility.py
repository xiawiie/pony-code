import importlib.util
import os
from pathlib import Path
import socket
import uuid

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "docker_sandbox_feasibility.py"
)
IMAGE_LOCK = SCRIPT.parents[1] / "docker" / "sandbox" / "image-inputs.lock.json"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "docker_sandbox_feasibility",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_code(module, code, callback):
    with pytest.raises(module.D1Error) as caught:
        callback()
    assert caught.value.code == code


def _plan(tmp_path):
    return {
        "sandbox_id": "sandbox_" + "a" * 32,
        "call_id": "call_" + "b" * 32,
        "reconciliation_token": "c" * 64,
        "container_name": "pico-d1-" + "d" * 24,
        "image_reference": "sha256:" + "e" * 64,
        "image_id": "sha256:" + "f" * 64,
        "workspace": str(tmp_path / "workspace"),
        "target_argv": ["/bin/sh", "-c", "printf ok"],
        "user": "501:20",
        "labels": {
            "io.pico.d1.managed": "true",
            "io.pico.d1.sandbox": "sandbox_" + "a" * 32,
            "io.pico.d1.call": "call_" + "b" * 32,
            "io.pico.d1.token": "c" * 64,
        },
    }


def _inspect_payload(plan):
    return {
        "Id": "f" * 64,
        "Image": plan["image_reference"],
        "Path": plan["target_argv"][0],
        "Args": plan["target_argv"][1:],
        "Config": {
            "Hostname": "pico-sandbox",
            "User": plan["user"],
            "Env": list(module_env()),
            "WorkingDir": "/workspace",
            "Labels": plan["labels"],
        },
        "HostConfig": {
            "Binds": None,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 256,
            "Memory": 2 * 1024**3,
            "MemorySwap": 2 * 1024**3,
            "NanoCpus": 2_000_000_000,
            "ShmSize": 64 * 1024**2,
            "Ulimits": [
                {"Name": "nofile", "Soft": 1024, "Hard": 1024},
                {"Name": "core", "Soft": 0, "Hard": 0},
            ],
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": {
                "/tmp": "rw,nosuid,nodev,exec,size=768m,mode=1777",
                "/home/pico": "rw,nosuid,nodev,noexec,size=64m,mode=700,uid=10001,gid=10001",
                "/run": "rw,nosuid,nodev,noexec,size=16m,mode=755,uid=10001,gid=10001",
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": plan["workspace"],
                    "Target": "/workspace",
                    "BindOptions": {
                        "Propagation": "rprivate",
                        "NonRecursive": True,
                    },
                }
            ],
            "IpcMode": "private",
            "PidMode": "",
            "UTSMode": "",
            "CgroupnsMode": "private",
            "UsernsMode": "",
            "Devices": [],
            "DeviceRequests": None,
            "PortBindings": {},
            "PublishAllPorts": False,
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": plan["workspace"],
                "Destination": "/workspace",
                "Mode": "rw,rprivate",
                "RW": True,
                "Propagation": "rprivate",
            }
        ],
        "NetworkSettings": {"Networks": {"none": {}}},
    }


def module_env():
    return (
        "PATH=/opt/pico-venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME=/home/pico",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PICO_SANDBOX=1",
        "PICO_WORKSPACE=/workspace",
        "PYTHONDONTWRITEBYTECODE=1",
        "TMPDIR=/tmp",
    )


def _passing_report(module):
    checks = [
        {
            "check_id": check_id,
            "status": "pass",
            "reason_code": "verified",
        }
        for check_id in module.MANDATORY_CHECK_IDS
    ]
    return {
        "record_type": "docker_sandbox_d1_run",
        "format_version": 1,
        "status": "passed",
        "reason_code": "mandatory_checks_passed",
        "candidate_digest": "sha256:" + "1" * 64,
        "policy_digest": "sha256:" + "2" * 64,
        "corpus_digest": module.CORPUS_DIGEST,
        "checks": checks,
        "mandatory_passed": len(checks),
        "mandatory_failed": 0,
        "target_started_count": 1,
        "container_create_count": 1,
        "host_fallback_count": 0,
        "residue_count": 0,
        "source_unchanged": True,
    }


def test_help_exposes_only_standalone_d1_actions():
    module = _load_script()
    parser = module.build_parser()

    help_text = parser.format_help()

    assert "status" in help_text
    assert "prepare-image" in help_text
    assert "calibrate" in help_text
    assert "run" in help_text
    assert "verify" in help_text
    assert "--sandbox" not in help_text


def test_prepare_image_parser_keeps_buildx_separate_from_status():
    module = _load_script()
    parser = module.build_parser()

    status = parser.parse_args(
        [
            "status",
            "--docker-cli",
            "/docker",
            "--socket",
            "/docker.sock",
            "--docker-config",
            "/config",
        ]
    )
    prepare = parser.parse_args(
        [
            "prepare-image",
            "--docker-cli",
            "/docker",
            "--buildx-cli",
            "/docker-buildx",
            "--socket",
            "/docker.sock",
            "--state-root",
            "/state",
            "--repo-root",
            "/repo",
            "--dockerfile",
            "/repo/Dockerfile",
            "--image-lock",
            "/repo/image-inputs.lock.json",
        ]
    )

    assert not hasattr(status, "buildx_cli")
    assert prepare.buildx_cli == "/docker-buildx"


def test_strict_json_rejects_duplicate_unknown_and_non_finite_values():
    module = _load_script()

    _assert_code(
        module,
        "artifact_invalid",
        lambda: module._decode_json(b'{"a":1,"a":2}', {"a"}),
    )
    _assert_code(
        module,
        "artifact_schema_invalid",
        lambda: module._decode_json(b'{"a":1,"b":2}', {"a"}),
    )
    _assert_code(
        module,
        "artifact_invalid",
        lambda: module._decode_json(b'{"a":NaN}', {"a"}),
    )


def test_image_lock_is_exact_and_complete():
    module = _load_script()

    lock, digest = module._load_image_lock(IMAGE_LOCK)

    assert digest.startswith("sha256:")
    assert lock["base_image"]["version"] == "3.12.13"
    assert lock["uv"]["version"] == "0.11.26"
    assert len(lock["debian_packages"]) == 18
    assert {item["name"] for item in lock["python_wheels"]} == {
        "iniconfig",
        "packaging",
        "pluggy",
        "pygments",
        "pytest",
        "ruff",
    }


def test_cached_asset_requires_exact_size_and_hash(tmp_path):
    module = _load_script()
    asset = {
        "filename": "asset.bin",
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "size": 3,
    }
    path = tmp_path / asset["filename"]
    path.write_bytes(b"abc")

    assert module._cached_asset_valid(path, asset)

    path.write_bytes(b"abd")
    assert not module._cached_asset_valid(path, asset)


def test_stale_run_cleanup_ignores_run_artifact(tmp_path):
    module = _load_script()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    artifact = state / "run-artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    stale = state / ("run-" + "a" * 24)
    stale.mkdir()
    (stale / "file").write_text("discard", encoding="utf-8")

    module._discard_stale_run_roots(state)

    assert artifact.exists()
    assert not stale.exists()


def test_image_inspect_requires_exact_empty_config():
    module = _load_script()
    payload = {
        "Id": "sha256:" + "a" * 64,
        "Architecture": "arm64",
        "Os": "linux",
        "Config": {
            "Entrypoint": None,
            "Cmd": None,
            "User": "10001:10001",
            "WorkingDir": "/workspace",
            "Env": list(module.GUEST_ENV),
            "Labels": dict(module._IMAGE_LABELS),
            "Volumes": None,
            "ExposedPorts": None,
            "Healthcheck": None,
        },
    }

    assert module._verify_image_inspect(payload) == {
        "image_reference": payload["Id"],
        "image_id": payload["Id"],
    }

    payload["Config"]["Cmd"] = ["python3"]
    _assert_code(
        module,
        "image_config_mismatch",
        lambda: module._verify_image_inspect(payload),
    )


def test_private_artifact_reader_rejects_symlink_hardlink_and_oversize(tmp_path):
    module = _load_script()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    artifact = root / "artifact.json"
    artifact.write_text('{"a":1}', encoding="utf-8")
    artifact.chmod(0o600)

    assert module._read_json_artifact(artifact, root, {"a"}, max_bytes=32) == {
        "a": 1
    }

    link = root / "link.json"
    link.symlink_to(artifact.name)
    _assert_code(
        module,
        "artifact_invalid",
        lambda: module._read_json_artifact(link, root, {"a"}, max_bytes=32),
    )

    hardlink = root / "hardlink.json"
    os.link(artifact, hardlink)
    _assert_code(
        module,
        "artifact_invalid",
        lambda: module._read_json_artifact(artifact, root, {"a"}, max_bytes=32),
    )

    hardlink.unlink()
    artifact.write_bytes(b"{" + b" " * 64 + b"}")
    _assert_code(
        module,
        "artifact_invalid",
        lambda: module._read_json_artifact(artifact, root, set(), max_bytes=32),
    )


def test_strict_empty_docker_config_is_read_only(tmp_path):
    module = _load_script()
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    config_file = config / "config.json"
    config_file.write_bytes(b"{}")
    config_file.chmod(0o600)
    before = module._directory_snapshot(config)

    identity = module._strict_empty_docker_config(config)

    assert identity["sha256"] == module._sha256_bytes(b"{}")
    assert module._directory_snapshot(config) == before


def test_strict_empty_docker_config_rejects_plugins_or_credentials(tmp_path):
    module = _load_script()
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(
        '{"auths":{"registry.example":{}}}',
        encoding="utf-8",
    )

    _assert_code(
        module,
        "docker_config_untrusted",
        lambda: module._strict_empty_docker_config(config),
    )

    (config / "config.json").write_text("{}", encoding="utf-8")
    (config / "cli-plugins").mkdir()
    _assert_code(
        module,
        "docker_config_untrusted",
        lambda: module._strict_empty_docker_config(config),
    )


def test_cli_identity_resolves_allowlisted_system_symlink():
    module = _load_script()
    shell = Path("/bin/sh")
    if not shell.exists():
        pytest.skip("POSIX shell unavailable")

    identity = module._freeze_cli(shell)

    assert Path(identity["resolved_path"]).is_file()
    assert identity["sha256"].startswith("sha256:")
    module._verify_cli(identity)


def test_socket_identity_is_canonical_and_revalidated():
    module = _load_script()
    socket_path = Path("/tmp") / f"pico-d1-{uuid.uuid4().hex}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    try:
        identity = module._freeze_socket(socket_path)
        assert identity["canonical_path"] == str(socket_path.resolve())
        module._verify_socket(identity)
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)

    _assert_code(
        module,
        "docker_endpoint_changed",
        lambda: module._verify_socket(identity),
    )


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (".git/config", "excluded_git"),
        (".pico/runs/a.json", "excluded_pico_state"),
        (".env", "sensitive_path"),
        ("nested/.env.local", "sensitive_path"),
        ("node_modules/pkg/a.js", "excluded_generated"),
        (".claude/settings.json", "excluded_agent_control"),
        ("src/main.py", ""),
    ],
)
def test_staging_filter_has_fixed_reasons(relative, expected):
    module = _load_script()
    assert module._staging_filter_reason(relative) == expected


def test_safe_env_template_is_allowed_unless_it_contains_known_secret():
    module = _load_script()

    assert module._staging_filter_reason(".env.example") == ""
    assert (
        module._content_filter_reason(
            ".env.example",
            b"TOKEN=known-value\n",
            (b"known-value",),
        )
        == "known_secret_content"
    )
    assert (
        module._content_filter_reason(
            "fixture.txt",
            b"-----BEGIN PRIVATE KEY-----\n"
            + b"A" * 160
            + b"\n-----END PRIVATE KEY-----\n",
        )
        == "high_confidence_secret"
    )
    assert (
        module._content_filter_reason(
            "scanner.py",
            b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        )
        == ""
    )


def test_staging_copy_is_stable_and_preserves_tracked_classification(tmp_path):
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.py").write_text("print('tracked')\n", encoding="utf-8")
    (source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=value\n", encoding="utf-8")
    destination = tmp_path / "staging"

    result = module.stage_source(
        source,
        destination,
        tracked_paths={"tracked.py"},
        candidate_paths={"tracked.py", "untracked.txt", ".env"},
        known_secrets=(b"value",),
    )

    assert (destination / "tracked.py").read_text() == "print('tracked')\n"
    assert (destination / "untracked.txt").read_text() == "untracked\n"
    assert not (destination / ".env").exists()
    assert result["tracked_paths"] == ["tracked.py"]
    assert result["untracked_paths"] == ["untracked.txt"]
    assert result["excluded_counts"] == {"sensitive_path": 1}
    assert result["tree_digest"].startswith("sha256:")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_staging_rejects_unsupported_source_entries(tmp_path, kind):
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    ordinary = source / "ordinary.txt"
    ordinary.write_text("data", encoding="utf-8")
    candidate = source / "candidate"
    if kind == "symlink":
        candidate.symlink_to(ordinary.name)
    elif kind == "hardlink":
        os.link(ordinary, candidate)
    else:
        os.mkfifo(candidate)

    _assert_code(
        module,
        "unsupported_workspace_entry",
        lambda: module.stage_source(
            source,
            tmp_path / "staging",
            candidate_paths={"candidate"},
        ),
    )


def test_staging_rejects_casefold_and_nfc_collisions(tmp_path):
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    (source / "A.txt").write_text("a", encoding="utf-8")
    (source / "a.txt").write_text("b", encoding="utf-8")

    _assert_code(
        module,
        "workspace_path_collision",
        lambda: module.stage_source(
            source,
            tmp_path / "staging",
            candidate_paths={"A.txt", "a.txt"},
        ),
    )


def test_create_argv_has_one_bind_and_fixed_security_contract(tmp_path):
    module = _load_script()
    plan = _plan(tmp_path)
    Path(plan["workspace"]).mkdir()

    argv = module._compile_create_argv(plan)

    assert argv[:2] == ["create", "--pull=never"]
    assert argv.count("--mount") == 1
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--pids-limit=256" in argv
    assert "--memory=2g" in argv
    assert "--memory-swap=2g" in argv
    assert "--cpus=2" in argv
    assert "--shm-size=64m" in argv
    assert "--ulimit=nofile=1024:1024" in argv
    assert "--ulimit=core=0:0" in argv
    assert "--log-driver=none" in argv
    assert not any("docker.sock" in item for item in argv)
    mount = argv[argv.index("--mount") + 1]
    assert "dst=/workspace" in mount
    assert ",rw" not in mount
    assert "bind-recursive=disabled" in mount
    assert argv[-len(plan["target_argv"]) :] == plan["target_argv"]


def test_inspect_reverse_proof_rejects_extra_mount_or_weak_resource(tmp_path):
    module = _load_script()
    plan = _plan(tmp_path)
    Path(plan["workspace"]).mkdir()
    payload = _inspect_payload(plan)

    module._verify_container_inspect(payload, plan)

    payload["Mounts"].append(dict(payload["Mounts"][0], Destination="/extra"))
    _assert_code(
        module,
        "container_contract_mismatch",
        lambda: module._verify_container_inspect(payload, plan),
    )
    payload = _inspect_payload(plan)
    payload["HostConfig"]["Memory"] = 0
    _assert_code(
        module,
        "container_contract_mismatch",
        lambda: module._verify_container_inspect(payload, plan),
    )


def test_run_artifact_requires_every_mandatory_check_and_zero_fallback():
    module = _load_script()
    report = _passing_report(module)

    module._validate_run_artifact(report)

    report["checks"][0]["status"] = "not_run"
    _assert_code(
        module,
        "mandatory_checks_incomplete",
        lambda: module._validate_run_artifact(report),
    )
    report = _passing_report(module)
    report["host_fallback_count"] = 1
    _assert_code(
        module,
        "mandatory_evidence_incomplete",
        lambda: module._validate_run_artifact(report),
    )


def test_approval_is_detached_and_binds_exact_run_artifact():
    module = _load_script()
    report = _passing_report(module)

    approval = module.build_feasibility_approval(
        report,
        artifact_digest="sha256:" + "3" * 64,
    )

    assert approval == {
        "record_type": "docker_sandbox_feasibility_approval",
        "format_version": 1,
        "status": "approved_for_implementation",
        "candidate_digest": report["candidate_digest"],
        "policy_digest": report["policy_digest"],
        "corpus_digest": module.CORPUS_DIGEST,
        "run_artifact_digest": "sha256:" + "3" * 64,
        "product_enablement": False,
    }


def test_fixture_apply_success_conflict_and_fault_rollback(tmp_path):
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    target = source / "value.txt"
    target.write_text("before", encoding="utf-8")
    baseline = module._file_state(target)

    assert module._apply_fixture_file(target, baseline, b"after") == "applied"
    assert target.read_bytes() == b"after"

    target.write_text("external", encoding="utf-8")
    assert module._apply_fixture_file(target, baseline, b"ignored") == "conflict"
    assert target.read_text() == "external"

    rollback_baseline = module._file_state(target)
    assert (
        module._apply_fixture_file(
            target,
            rollback_baseline,
            b"candidate",
            inject_fault=True,
        )
        == "failed_rolled_back"
    )
    assert target.read_text() == "external"


def test_no_d1_approval_symbol_is_imported_by_product_runtime():
    forbidden = "docker_sandbox_feasibility_approval"
    for path in (Path(__file__).resolve().parents[1] / "pico").rglob("*.py"):
        assert forbidden not in path.read_text(encoding="utf-8")
