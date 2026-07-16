from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

import pytest

import pico.sandbox.docker as docker_module
from pico.sandbox.docker import (
    compile_create_argv,
    default_image_manifest_path,
    discover_local_docker,
    DockerClient,
    DockerCommandResult,
    DockerExecutionOutcome,
    DockerSandboxError,
    DockerSandboxRunner,
    DOCKER_POLICY,
    GUEST_ENV,
    ensure_runtime_docker_config,
    load_image_manifest,
    measure_workspace,
    MOUNT_POLICY_DIGEST,
    POLICY_DIGEST,
    RESOURCE_POLICY_DIGEST,
    verify_container_inspect,
    verify_image_inspect,
)
from pico.sandbox.session import SandboxSessionStore, WorkspaceView


CONTAINER_ID = "e" * 64
CLIENT_DIGEST = "sha256:" + "f" * 64


def _session_metadata():
    image = load_image_manifest(default_image_manifest_path())
    return {
        "engine": {
            "endpoint_hash": CLIENT_DIGEST,
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "1" * 64,
        },
        "image": {
            "reference": image.registry_reference or image.reference,
            "manifest_digest": image.reference,
            "image_id": image.image_id,
            "platform": image.platform,
        },
        "policy": {
            "version": 1,
            "digest": POLICY_DIGEST,
            "network": "none",
            "mount_digest": MOUNT_POLICY_DIGEST,
            "resource_digest": RESOURCE_POLICY_DIGEST,
        },
    }


def _result(
    *,
    exit_code=0,
    stdout=b"",
    stderr=b"",
    timed_out=False,
    stdout_truncated=False,
    stderr_truncated=False,
):
    return DockerCommandResult(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _bootstrap(request):
    view = request.workspace_view
    tracked_paths = request.tracked_paths
    assert isinstance(view, WorkspaceView)
    assert isinstance(tracked_paths, tuple)
    git = view.physical_root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/pico-sandbox\n", encoding="utf-8")
    return "a" * 40


def _session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    store = SandboxSessionStore(tmp_path / "sandboxes")
    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        **_session_metadata(),
    )
    return source, store, session


def _container_payload(plan, *, started=False, exit_code=0, oom=False):
    state = {
        "Dead": False,
        "Error": "",
        "ExitCode": exit_code,
        "FinishedAt": (
            "2026-07-13T00:00:01Z" if started else "0001-01-01T00:00:00Z"
        ),
        "OOMKilled": oom,
        "Paused": False,
        "Pid": 0,
        "Restarting": False,
        "Running": False,
        "StartedAt": (
            "2026-07-13T00:00:00Z" if started else "0001-01-01T00:00:00Z"
        ),
        "Status": "exited" if started else "created",
    }
    return {
        "Args": list(plan.target_argv[1:]),
        "Config": {
            "Entrypoint": None,
            "Env": list(plan.env),
            "ExposedPorts": None,
            "Healthcheck": None,
            "Hostname": "pico-sandbox",
            "Labels": plan.label_map,
            "User": plan.user,
            "Volumes": None,
            "WorkingDir": "/workspace",
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": None,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "CgroupnsMode": "private",
            "DeviceRequests": None,
            "Devices": [],
            "IpcMode": "private",
            "LogConfig": {"Type": "none", "Config": {}},
            "Memory": DOCKER_POLICY["memory_bytes"],
            "MemorySwap": DOCKER_POLICY["memory_swap_bytes"],
            "Mounts": [
                {
                    "BindOptions": {
                        "NonRecursive": True,
                        "Propagation": "rprivate",
                    },
                    "Source": plan.workspace,
                    "Target": "/workspace",
                    "Type": "bind",
                }
            ],
            "NanoCpus": DOCKER_POLICY["nano_cpus"],
            "NetworkMode": "none",
            "PidMode": "",
            "PidsLimit": DOCKER_POLICY["pids_limit"],
            "PortBindings": {},
            "Privileged": False,
            "PublishAllPorts": False,
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "SecurityOpt": ["no-new-privileges:true"],
            "ShmSize": DOCKER_POLICY["shm_bytes"],
            "Tmpfs": DOCKER_POLICY["tmpfs"],
            "UTSMode": "",
            "Ulimits": [
                {"Hard": 0, "Name": "core", "Soft": 0},
                {"Hard": 1024, "Name": "nofile", "Soft": 1024},
            ],
            "UsernsMode": "",
        },
        "Id": CONTAINER_ID,
        "Image": plan.image_manifest_digest,
        "ImageManifestDescriptor": {
            "annotations": {"config.digest": plan.image_id},
            "digest": plan.image_manifest_digest,
        },
        "Mounts": [
            {
                "Destination": "/workspace",
                "Propagation": "rprivate",
                "RW": True,
                "Source": plan.workspace,
                "Type": "bind",
            }
        ],
        "Name": "/" + plan.container_name,
        "NetworkSettings": {"Networks": {"none": {}}},
        "Path": plan.target_argv[0],
        "State": state,
    }


def _image_payload(image):
    return [{
        "Architecture": image.architecture,
        "Config": {
            "Cmd": None,
            "Entrypoint": None,
            "Env": list(image.env),
            "ExposedPorts": None,
            "Healthcheck": None,
            "Labels": image.label_map,
            "StopSignal": "",
            "User": image.user,
            "Volumes": None,
            "WorkingDir": image.working_dir,
        },
        "Descriptor": {
            "annotations": {"config.digest": image.image_id},
            "digest": image.reference,
        },
        "Id": image.reference,
        "Os": image.operating_system,
    }]


def test_image_inspect_accepts_real_containerd_shape_without_descriptor():
    image = load_image_manifest(default_image_manifest_path())
    payload = _image_payload(image)
    payload[0].pop("Descriptor")

    verify_image_inspect(payload, image)


def test_image_inspect_without_descriptor_requires_manifest_id():
    image = load_image_manifest(default_image_manifest_path())
    payload = _image_payload(image)
    payload[0].pop("Descriptor")
    payload[0]["Id"] = image.image_id

    with pytest.raises(DockerSandboxError, match="sandbox_image_identity_mismatch"):
        verify_image_inspect(payload, image)


def test_image_inspect_rejects_present_but_incomplete_descriptor():
    image = load_image_manifest(default_image_manifest_path())
    payload = _image_payload(image)
    payload[0]["Descriptor"] = {}

    with pytest.raises(DockerSandboxError, match="sandbox_image_identity_mismatch"):
        verify_image_inspect(payload, image)


class FakeDockerClient:
    def __init__(
        self,
        *,
        create_stdout=None,
        timeout=False,
        exit_code=0,
        oom=False,
        contract_mismatch=False,
        cleanup_failure=False,
        start_delay=0,
        create_error=None,
        start_error=None,
        terminal_inspect_error=False,
        stop_failure=False,
        readiness_error=None,
    ):
        self.create_stdout = create_stdout
        self.timeout = timeout
        self.target_exit_code = exit_code
        self.oom = oom
        self.contract_mismatch = contract_mismatch
        self.cleanup_failure = cleanup_failure
        self.start_delay = start_delay
        self.create_error = create_error
        self.start_error = start_error
        self.terminal_inspect_error = terminal_inspect_error
        self.stop_failure = stop_failure
        self.readiness_error = readiness_error
        self.plan = None
        self.exists = False
        self.started = False
        self.stopped = False
        self.commands = []

    def identity_digest(self):
        return CLIENT_DIGEST

    def require_ready(self, _image):
        if self.readiness_error:
            raise DockerSandboxError(self.readiness_error)
        return {"status": "ready"}

    def command(self, args, **_kwargs):
        args = list(args)
        self.commands.append(args)
        if args[0] == "create":
            if self.create_error:
                raise DockerSandboxError(self.create_error)
            self.exists = True
            stdout = (
                self.create_stdout
                if self.create_stdout is not None
                else (CONTAINER_ID + "\n").encode()
            )
            return _result(stdout=stdout)
        if args[:2] == ["container", "inspect"]:
            if not self.exists:
                return _result(exit_code=1, stderr=b"not found")
            if self.terminal_inspect_error and self.started:
                return _result(exit_code=1, stderr=b"inspect failed")
            payload = _container_payload(
                self.plan,
                started=self.started or self.stopped,
                exit_code=self.target_exit_code,
                oom=self.oom,
            )
            if self.contract_mismatch:
                payload["HostConfig"]["NetworkMode"] = "bridge"
            return _result(stdout=json.dumps(payload).encode())
        if args[:3] == ["container", "start", "--attach"]:
            if self.start_error:
                raise DockerSandboxError(self.start_error)
            if self.start_delay:
                time.sleep(self.start_delay)
            if not self.stopped:
                self.started = True
            return _result(
                stdout=b"target-output",
                timed_out=self.timeout,
            )
        if args[:2] == ["container", "stop"]:
            if self.stop_failure:
                return _result(exit_code=1, stderr=b"stop failed")
            self.stopped = True
            return _result(stdout=(CONTAINER_ID + "\n").encode())
        if args[:2] == ["container", "kill"]:
            self.stopped = True
            return _result(stdout=(CONTAINER_ID + "\n").encode())
        if args[:3] == ["container", "rm", "--force"]:
            if self.cleanup_failure:
                return _result(exit_code=1, stderr=b"failed")
            self.exists = False
            return _result(stdout=(CONTAINER_ID + "\n").encode())
        if args[:3] == ["container", "ls", "--all"]:
            return _result(
                stdout=(CONTAINER_ID + "\n").encode() if self.exists else b""
            )
        raise AssertionError(args)


def _runner(tmp_path, client=None, **kwargs):
    source, store, session = _session(tmp_path)
    image = load_image_manifest(default_image_manifest_path())
    client = client or FakeDockerClient()
    runner = DockerSandboxRunner(client, store, image, **kwargs)
    plan = runner.compile(session, ["/bin/sh", "-c", "printf ok"], timeout=5)
    client.plan = plan
    return source, store, session, runner, plan, client


def _persist_crashed_call(store, session, runner, plan, *, container_id=""):
    runner._persist_call_plan(session.state_root, plan)
    store.begin_call(
        session.state_root,
        call_id=plan.call_id,
        reconciliation_token=plan.reconciliation_token,
        container_name=plan.container_name,
        expected_labels=plan.label_map,
        plan_digest=plan.execution_plan_digest,
    )
    if container_id:
        store.record_container_id(session.state_root, container_id)


def test_packaged_image_manifest_binds_d1_policy_and_image():
    image = load_image_manifest(default_image_manifest_path())

    assert POLICY_DIGEST == "sha256:96aa648358b4e8efa83c5d1792b980518198844e7993893b65307c12a7a1c2f6"
    assert image.policy_digest == POLICY_DIGEST
    assert image.image_set_digest.startswith("sha256:")
    assert image.reference == "sha256:61f5e86e344d4053b8f6c7053c965b2cde7fc5e77777974e6237ad2e4ec36904"
    assert image.image_id == "sha256:4b8538d9c53897e45fd0aa798f78dcc29795956f208efeed0bb7d5662f933ca8"
    assert image.env == GUEST_ENV


def test_packaged_image_manifest_rejects_unreleased_amd64_target():
    with pytest.raises(DockerSandboxError, match="sandbox_image_not_released"):
        load_image_manifest(
            default_image_manifest_path(),
            target_platform="linux/amd64",
        )


def test_image_manifest_rejects_unknown_and_duplicate_fields(tmp_path):
    value = json.loads(default_image_manifest_path().read_text())
    value["unexpected"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DockerSandboxError, match="sandbox_image_identity_mismatch"):
        load_image_manifest(path)

    path.write_text('{"record_type":1,"record_type":2}', encoding="utf-8")
    with pytest.raises(DockerSandboxError, match="sandbox_image_identity_mismatch"):
        load_image_manifest(path)


def test_execution_plan_and_create_argv_are_frozen(tmp_path):
    source, _store, session, runner, plan, _client = _runner(tmp_path)

    argv = compile_create_argv(plan)

    assert argv[0] == "create"
    assert "--pull=never" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "bind-recursive=disabled" in argv[argv.index("--mount") + 1]
    assert str(source) not in "\0".join(argv)
    assert argv[-3:] == ["/bin/sh", "-c", "printf ok"]
    assert argv.index(plan.image_reference) < len(argv) - 3
    assert sum(item.startswith("--mount") for item in argv) == 1

    with pytest.raises(DockerSandboxError, match="approved_execution_changed"):
        compile_create_argv(replace(plan, timeout=4))
    with pytest.raises(DockerSandboxError, match="approved_execution_changed"):
        runner.compile(
            session,
            ["/bin/sh", "-c", "printf ok"],
            logical_intent_digest="sha256:" + "0" * 64,
        )


def test_create_uses_registry_manifest_and_keeps_oci_identities_distinct(tmp_path):
    _source, _store, session, _runner_value, _plan, _client = _runner(tmp_path)
    image = load_image_manifest(default_image_manifest_path())
    released = replace(
        image,
        registry_reference="registry.example/pico@" + image.reference,
    )

    plan = docker_module.compile_execution_plan(
        session,
        released,
        CLIENT_DIGEST,
        ["/bin/sh", "-c", "printf ok"],
    )
    argv = compile_create_argv(plan)

    assert plan.image_reference == released.registry_reference
    assert plan.image_manifest_digest == released.reference
    assert plan.image_id == released.image_id
    assert argv[-4] == released.registry_reference


def test_runner_rejects_rehashed_plan_for_a_different_image(tmp_path):
    _source, _store, session, runner, plan, client = _runner(tmp_path)
    changed = replace(
        plan,
        image_reference="sha256:" + "0" * 64,
        execution_plan_digest="",
    )
    changed = replace(
        changed,
        execution_plan_digest=docker_module._sha256(
            docker_module._canonical_json(changed.digest_payload())
        ),
    )

    with pytest.raises(DockerSandboxError, match="approved_execution_changed"):
        runner.execute(session, changed)

    assert not (session.state_root / "active-call-plan.json").exists()
    assert not any(args[0] == "create" for args in client.commands)


def test_status_uses_one_capability_payload_and_exact_image():
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        def json_command(self, args):
            if args[0] == "version":
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": "1.52", "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": ["name=seccomp,profile=builtin"],
            }

        def command(self, args):
            assert args[0:2] == ["image", "inspect"]
            return _result(stdout=json.dumps(_image_payload(image)).encode())

    status = DockerClient.status(StatusClient(), image, host_system="Darwin")

    assert status["status"] == "ready"
    assert status["platform_profile"] == "desktop_vm"
    assert status["image"] == {
        "present": True,
        "digest_match": True,
        "platform_match": True,
    }
    assert status["network_performed"] is False
    assert status["mutation_performed"] is False


@pytest.mark.parametrize(
    ("case", "expected_image"),
    (
        (
            "missing",
            {"present": False, "digest_match": False, "platform_match": True},
        ),
        (
            "digest",
            {"present": True, "digest_match": False, "platform_match": True},
        ),
        (
            "platform",
            {"present": True, "digest_match": False, "platform_match": True},
        ),
        (
            "label",
            {"present": True, "digest_match": False, "platform_match": True},
        ),
    ),
)
def test_status_reports_exact_image_failure(case, expected_image):
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        def json_command(self, args):
            if args[0] == "version":
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": "1.52", "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": ["name=seccomp,profile=builtin"],
            }

        def command(self, _args):
            if case == "missing":
                return _result(exit_code=1, stderr=b"not found")
            payload = _image_payload(image)
            if case == "digest":
                payload[0]["Id"] = "sha256:" + "0" * 64
            elif case == "platform":
                payload[0]["Architecture"] = "amd64"
            else:
                payload[0]["Config"]["Labels"] = {
                    **payload[0]["Config"]["Labels"],
                    "io.pico.sandbox.managed": "false",
                }
            return _result(stdout=json.dumps(payload).encode())

    status = DockerClient.status(StatusClient(), image, host_system="Darwin")

    assert status["status"] == "not_ready"
    assert status["reason_code"] == (
        "sandbox_image_missing"
        if case == "missing"
        else "sandbox_image_identity_mismatch"
    )
    assert status["image"] == expected_image


@pytest.mark.parametrize(
    (
        "host_system",
        "security_options",
        "api_version",
        "expected",
    ),
    (
        ("Linux", ["name=seccomp,profile=builtin"], "1.52", "docker_rootless_required"),
        (
            "Linux",
            ["name=notrootless", "name=seccomp,profile=builtin"],
            "1.52",
            "docker_rootless_required",
        ),
        ("Linux", ["name=rootless"], "1.52", "docker_seccomp_unavailable"),
        (
            "Linux",
            ["name=rootless", "name=seccomp,profile=builtin"],
            "1.43",
            "docker_server_unsupported",
        ),
    ),
)
def test_status_reports_exact_unsupported_profile_reason(
    host_system,
    security_options,
    api_version,
    expected,
):
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        def json_command(self, args):
            if args[0] == "version":
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": api_version, "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": security_options,
            }

        def command(self, _args):
            return _result(stdout=json.dumps(_image_payload(image)).encode())

    status = DockerClient.status(StatusClient(), image, host_system=host_system)

    assert status["status"] == "not_ready"
    assert status["reason_code"] == expected


@pytest.mark.parametrize(
    "security_options",
    (
        {"name=rootless": True, "name=seccomp,profile=builtin": True},
        ["name=seccomp,profile=builtin", {"name=rootless": True}],
    ),
)
def test_status_rejects_malformed_security_options(security_options):
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        def json_command(self, args):
            if args[0] == "version":
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": "1.52", "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": security_options,
            }

    with pytest.raises(DockerSandboxError, match="docker_server_unsupported"):
        DockerClient.status(StatusClient(), image, host_system="Linux")


@pytest.mark.parametrize(
    ("seccomp_option", "expected_status", "expected_profile"),
    (
        ("name=seccomp,profile=builtin", "ready", "builtin"),
        ("name=seccomp,profile=default", "ready", "default"),
        ("name=seccomp,profile=unconfined", "not_ready", "unavailable"),
        ("name=not-seccomp,profile=builtin", "not_ready", "unavailable"),
    ),
)
def test_status_requires_exact_builtin_or_default_seccomp(
    seccomp_option,
    expected_status,
    expected_profile,
):
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        def json_command(self, args):
            if args[0] == "version":
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": "1.52", "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": [seccomp_option],
            }

        def command(self, _args):
            return _result(stdout=json.dumps(_image_payload(image)).encode())

    status = DockerClient.status(StatusClient(), image, host_system="Darwin")

    assert status["status"] == expected_status
    assert status["security"]["seccomp"] == expected_profile
    assert status["reason_code"] == (
        "ready" if expected_status == "ready" else "docker_seccomp_unavailable"
    )


@pytest.mark.parametrize("failure", ("timeout", "truncated", "nonzero"))
def test_status_reports_image_inspect_daemon_failure(failure):
    image = load_image_manifest(default_image_manifest_path())

    class StatusClient:
        version_calls = 0

        def json_command(self, args):
            if args[0] == "version":
                self.version_calls += 1
                if failure == "nonzero" and self.version_calls == 2:
                    raise DockerSandboxError("docker_daemon_unavailable")
                return {
                    "Client": {"Version": "29.5.2"},
                    "Server": {"ApiVersion": "1.52", "Version": "29.5.2"},
                }
            return {
                "Architecture": "aarch64",
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "OSType": "linux",
                "PidsLimit": True,
                "SecurityOptions": ["name=seccomp,profile=builtin"],
            }

        def command(self, _args):
            return _result(
                exit_code=1 if failure == "nonzero" else 0,
                timed_out=failure == "timeout",
            ) if failure != "truncated" else DockerCommandResult(
                exit_code=0,
                timed_out=False,
                stdout=b"{}",
                stderr=b"",
                stdout_bytes=2,
                stderr_bytes=0,
                stdout_truncated=True,
                stderr_truncated=False,
            )

    with pytest.raises(DockerSandboxError, match="docker_daemon_unavailable"):
        DockerClient.status(StatusClient(), image, host_system="Darwin")


@pytest.mark.parametrize(
    "reason",
    (
        "docker_daemon_unavailable",
        "docker_rootless_required",
        "docker_seccomp_unavailable",
        "docker_server_unsupported",
        "sandbox_image_missing",
        "sandbox_image_identity_mismatch",
    ),
)
def test_readiness_failures_prevent_call_state_and_target(
    tmp_path,
    reason,
):
    client = FakeDockerClient(readiness_error=reason)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    manifest_before = (session.state_root / "manifest.json").read_bytes()

    with pytest.raises(DockerSandboxError, match=reason):
        runner.execute(session, plan)

    assert (session.state_root / "manifest.json").read_bytes() == manifest_before
    assert not (session.state_root / "active-call-plan.json").exists()
    assert store.inspect(session.state_root).state == "ready"
    assert client.commands == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value["HostConfig"].update(NetworkMode="bridge"),
        lambda value: value["Config"].update(User="0:0"),
        lambda value: value["Mounts"].append(dict(value["Mounts"][0])),
        lambda value: value.update(Image=value["ImageManifestDescriptor"]["annotations"]["config.digest"]),
        lambda value: value["ImageManifestDescriptor"]["annotations"].update(
            {"config.digest": "sha256:" + "0" * 64}
        ),
    ),
)
def test_container_inspect_rejects_any_contract_mismatch(tmp_path, mutation):
    _source, _store, _session, _runner_value, plan, _client = _runner(tmp_path)
    payload = _container_payload(plan)
    verify_container_inspect(payload, plan, expected_id=CONTAINER_ID)
    mutated = deepcopy(payload)
    mutation(mutated)

    with pytest.raises(DockerSandboxError, match="container_contract_mismatch"):
        verify_container_inspect(mutated, plan, expected_id=CONTAINER_ID)


def test_runner_success_uses_one_create_and_start_and_cleans(tmp_path):
    source, store, session, runner, plan, client = _runner(tmp_path)

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "completed"
    assert outcome.target_started is True
    assert outcome.exit_code == 0
    assert outcome.stdout == b"target-output"
    assert outcome.cleanup_status == "completed"
    assert outcome.residue_detected is False
    assert sum(args[0] == "create" for args in client.commands) == 1
    assert sum(args[:3] == ["container", "start", "--attach"] for args in client.commands) == 1
    assert store.inspect(session.state_root).state == "ready"
    assert (source / "README.md").read_text() == "source\n"


def test_runner_persists_private_plan_before_active_manifest(tmp_path, monkeypatch):
    _source, store, session, runner, plan, _client = _runner(tmp_path)
    original = store.begin_call

    def assert_plan_precedes_manifest(*args, **kwargs):
        path = session.state_root / "active-call-plan.json"
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
        assert plan.execution_plan_digest.encode("ascii") in path.read_bytes()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "begin_call", assert_plan_precedes_manifest)

    outcome = runner.execute(session, plan)

    assert outcome.cleanup_status == "completed"


def test_runner_reconstructs_crashed_call_and_cleans_exact_container(tmp_path):
    _source, store, session, runner, plan, client = _runner(tmp_path)
    _persist_crashed_call(store, session, runner, plan)
    client.exists = True
    client.started = True

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"]["reconciliation"] == {
        "status": "review_required",
        "target_started": True,
        "cleanup_status": "completed",
        "error_code": "target_started_before_reconciliation",
    }
    assert client.exists is False
    assert any(
        args[:3] == ["container", "rm", "--force"] for args in client.commands
    )


def test_crash_cleanup_uses_current_control_plane_without_image_readiness(tmp_path):
    _source, store, session, runner, plan, client = _runner(tmp_path)
    _persist_crashed_call(store, session, runner, plan)
    client.exists = True
    client.identity_digest = lambda: "sha256:" + "a" * 64
    client.readiness_error = "sandbox_image_missing"

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "ready"
    assert reconciled.manifest["active_call"] is None
    assert client.exists is False


@pytest.mark.parametrize(
    ("started", "inspect_unknown", "target_started", "cleanup_status"),
    (
        (True, False, True, "completed"),
        (True, True, None, "not_attempted"),
    ),
)
def test_started_or_unknown_crash_stays_bound_for_review(
    tmp_path,
    started,
    inspect_unknown,
    target_started,
    cleanup_status,
):
    client = FakeDockerClient(terminal_inspect_error=inspect_unknown)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    _persist_crashed_call(store, session, runner, plan, container_id=CONTAINER_ID)
    client.exists = True
    client.started = started

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"]["container_id"] == CONTAINER_ID
    assert reconciled.manifest["active_call"]["reconciliation"] == {
        "status": "review_required",
        "target_started": target_started,
        "cleanup_status": cleanup_status,
        "error_code": (
            "target_started_before_reconciliation"
            if target_started is True
            else "target_start_state_unknown"
        ),
    }


@pytest.mark.parametrize("recorded_id", (False, True))
def test_runner_crash_reconciliation_handles_zero_container_cardinality(
    tmp_path,
    recorded_id,
):
    _source, store, session, runner, plan, _client = _runner(tmp_path)
    _persist_crashed_call(
        store,
        session,
        runner,
        plan,
        container_id=CONTAINER_ID if recorded_id else "",
    )

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    if recorded_id:
        assert reconciled.state == "review_required"
        assert reconciled.manifest["active_call"]["container_id"] == CONTAINER_ID
        assert reconciled.manifest["active_call"]["reconciliation"] == {
            "status": "review_required",
            "target_started": None,
            "cleanup_status": "completed",
            "error_code": "target_start_state_unknown",
        }
    else:
        assert reconciled.state == "ready"
        assert reconciled.manifest["active_call"] is None
    list_commands = [
        args for args in _client.commands if args[:3] == ["container", "ls", "--all"]
    ]
    assert len(list_commands) == (2 if recorded_id else 1)
    if recorded_id:
        assert list_commands[-1][-2:] == ["--filter", "id=" + CONTAINER_ID]


@pytest.mark.parametrize(
    "exact_result",
    ("present", "query_failed", "timed_out", "stdout_truncated", "stderr_truncated"),
)
def test_runner_recorded_id_absence_requires_exact_query_proof(
    tmp_path,
    exact_result,
):
    class LabelMissClient(FakeDockerClient):
        def command(self, args, **kwargs):
            args = list(args)
            if args[:3] == ["container", "ls", "--all"] and any(
                item.startswith("label=") for item in args
            ):
                self.commands.append(args)
                return _result()
            if args[-2:] == ["--filter", "id=" + CONTAINER_ID]:
                self.commands.append(args)
                if exact_result == "present":
                    return _result(stdout=(CONTAINER_ID + "\n").encode())
                if exact_result == "query_failed":
                    return _result(exit_code=1, stderr=b"query failed")
                if exact_result == "timed_out":
                    return _result(timed_out=True)
                result = _result(stdout=b"")
                return replace(result, **{exact_result: True})
            return super().command(args, **kwargs)

    client = LabelMissClient()
    _source, store, session, runner, plan, client = _runner(tmp_path, client=client)
    _persist_crashed_call(
        store,
        session,
        runner,
        plan,
        container_id=CONTAINER_ID,
    )
    client.exists = True

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"]["container_id"] == CONTAINER_ID
    assert client.exists is True
    assert not any(
        args[:3] == ["container", "rm", "--force"] for args in client.commands
    )


def test_runner_crash_reconciliation_refuses_ambiguous_containers(tmp_path):
    class AmbiguousClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[:3] == ["container", "ls", "--all"]:
                self.commands.append(list(args))
                return _result(stdout=(CONTAINER_ID + "\n" + "d" * 64 + "\n").encode())
            return super().command(args, **kwargs)

    client = AmbiguousClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    _persist_crashed_call(store, session, runner, plan)
    client.exists = True

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert client.exists is True
    assert not any(
        args[:3] == ["container", "rm", "--force"] for args in client.commands
    )


@pytest.mark.parametrize("mutation", ("missing", "tampered"))
def test_runner_crash_reconciliation_refuses_missing_or_tampered_plan(
    tmp_path,
    mutation,
):
    _source, store, session, runner, plan, client = _runner(tmp_path)
    _persist_crashed_call(store, session, runner, plan)
    path = session.state_root / "active-call-plan.json"
    if mutation == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes().replace(b"/bin/sh", b"/bin/xx"))
    client.exists = True

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"] is not None
    assert client.exists is True
    assert not any(args[0] == "container" for args in client.commands)


def test_runner_retries_identity_bound_cleanup_from_review_state(tmp_path):
    client = FakeDockerClient(cleanup_failure=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    first = runner.execute(session, plan)
    assert first.cleanup_status == "failed"
    client.cleanup_failure = False

    reconciled = runner.reconcile_session(store.inspect(session.state_root))

    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"]["reconciliation"] == {
        "status": "review_required",
        "target_started": True,
        "cleanup_status": "completed",
        "error_code": "target_started_before_reconciliation",
    }
    assert client.exists is False


def test_synthetic_git_bootstrap_returns_to_creating_state(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    store = SandboxSessionStore(tmp_path / "sandboxes")
    image = load_image_manifest(default_image_manifest_path())
    client = FakeDockerClient()
    runner = DockerSandboxRunner(client, store, image)

    def execute(session, plan, *, expected_state):
        assert session.state == "creating"
        assert expected_state == "creating"
        store.begin_call(
            session.state_root,
            call_id=plan.call_id,
            reconciliation_token=plan.reconciliation_token,
            container_name=plan.container_name,
            expected_labels=plan.label_map,
            plan_digest=plan.execution_plan_digest,
            return_state="creating",
        )
        workspace = Path(plan.workspace)
        (workspace / plan.target_argv[-1].removeprefix("/workspace/")).unlink()
        git = workspace / ".git"
        git.mkdir()
        (git / "HEAD").write_text(
            "ref: refs/heads/pico-sandbox\n",
            encoding="utf-8",
        )
        store.finish_call(session.state_root)
        return DockerExecutionOutcome(
            stdout=("a" * 40 + "\n").encode(),
            stderr=b"",
            stdout_bytes=41,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            exit_code=0,
            timed_out=False,
            runner_executed=True,
            target_started=True,
            container_created=True,
            sandbox_outcome="completed",
            cleanup_status="completed",
            residue_detected=False,
            error_code="",
        )

    runner.execute = execute

    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=runner.bootstrap_git,
        **_session_metadata(),
    )

    assert session.state == "ready"
    assert session.manifest["execution"]["synthetic_git_commit"] == "a" * 40
    assert session.manifest["active_call"] is None


@pytest.mark.parametrize(
    ("client", "expected"),
    (
        (FakeDockerClient(exit_code=17), "completed"),
        (FakeDockerClient(timeout=True), "timeout"),
        (FakeDockerClient(exit_code=137, oom=True), "oom_killed"),
    ),
)
def test_runner_uses_inspected_exit_timeout_and_oom_truth(
    tmp_path,
    client,
    expected,
):
    _source, _store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == expected
    assert outcome.exit_code == client.target_exit_code
    assert outcome.target_started is True
    assert outcome.cleanup_status == "completed"


def test_contract_mismatch_never_starts_target_and_is_cleaned(tmp_path):
    client = FakeDockerClient(contract_mismatch=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "target_not_started"
    assert outcome.error_code == "container_contract_mismatch"
    assert not any(
        args[:3] == ["container", "start", "--attach"]
        for args in client.commands
    )
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


def test_malformed_create_response_reconciles_and_cleans_without_start(tmp_path):
    client = FakeDockerClient(create_stdout=b"not-an-id\n")
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "target_not_started"
    assert outcome.error_code == "container_create_failed"
    assert outcome.container_created is True
    assert client.exists is False
    assert not any(
        args[:3] == ["container", "start", "--attach"]
        for args in client.commands
    )
    assert store.inspect(session.state_root).state == "ready"


def test_create_cli_failure_reconciles_zero_containers_to_ready(tmp_path):
    client = FakeDockerClient(create_error="docker_daemon_unavailable")
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.error_code == "docker_daemon_unavailable"
    assert outcome.target_started is False
    assert outcome.cleanup_status == "completed"
    assert store.inspect(session.state_root).state == "ready"


@pytest.mark.parametrize(
    "failure",
    ("timed_out", "nonzero", "stdout_truncated", "stderr_truncated"),
)
def test_create_result_requires_exact_bounded_success(tmp_path, failure):
    class FaultClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[0] == "create":
                self.commands.append(list(args))
                return _result(
                    stdout=(CONTAINER_ID + "\n").encode(),
                    exit_code=1 if failure == "nonzero" else 0,
                    timed_out=failure == "timed_out",
                    stdout_truncated=failure == "stdout_truncated",
                    stderr_truncated=failure == "stderr_truncated",
                )
            return super().command(args, **kwargs)

    client = FaultClient()
    _source, store, session, runner, plan, _client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.error_code == "container_create_failed"
    assert outcome.target_started is False
    assert outcome.cleanup_status == "completed"
    assert store.inspect(session.state_root).state == "ready"


def test_start_failure_cleans_identity_bound_container(tmp_path):
    client = FakeDockerClient(start_error="docker_daemon_unavailable")
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "target_not_started"
    assert outcome.cleanup_status == "completed"
    assert outcome.residue_detected is False
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


@pytest.mark.parametrize(
    "failure",
    ("exception", "timed_out", "nonzero", "stdout_truncated", "stderr_truncated"),
)
def test_runner_terminal_inspect_fail_closed(tmp_path, failure):
    class FaultClient(FakeDockerClient):
        def command(self, args, **kwargs):
            args = list(args)
            if args[:2] == ["container", "inspect"] and self.started:
                self.commands.append(args)
                if failure == "exception":
                    raise DockerSandboxError("docker_daemon_unavailable")
                return _result(
                    exit_code=1 if failure == "nonzero" else 0,
                    timed_out=failure == "timed_out",
                    stdout_truncated=failure == "stdout_truncated",
                    stderr_truncated=failure == "stderr_truncated",
                )
            return super().command(args, **kwargs)

    client = FaultClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "target_not_started"
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert client.exists is True
    assert store.inspect(session.state_root).state == "review_required"


def test_runner_recovers_target_truth_after_start_attach_lost_response(tmp_path):
    class LostResponseClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[:3] == ["container", "start", "--attach"]:
                self.commands.append(list(args))
                self.started = True
                raise DockerSandboxError("docker_daemon_unavailable")
            return super().command(args, **kwargs)

    client = LostResponseClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "container_runtime_failed"
    assert outcome.error_code == "docker_daemon_unavailable"
    assert outcome.runner_executed is True
    assert outcome.target_started is True
    assert outcome.cleanup_status == "completed"
    assert outcome.residue_detected is False
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


def test_terminal_inspect_failure_blocks_then_reconciles_after_recovery(tmp_path):
    client = FakeDockerClient(terminal_inspect_error=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "target_not_started"
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert store.inspect(session.state_root).state == "review_required"
    client.terminal_inspect_error = False
    reconciled = runner.reconcile_session(store.inspect(session.state_root))
    assert reconciled.state == "review_required"
    assert reconciled.manifest["active_call"]["reconciliation"] == {
        "status": "review_required",
        "target_started": True,
        "cleanup_status": "completed",
        "error_code": "target_started_before_reconciliation",
    }
    assert client.exists is False


def test_timeout_kills_immediately_before_cleanup(tmp_path):
    client = FakeDockerClient(timeout=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "timeout"
    assert outcome.cleanup_status == "completed"
    assert not any(args[:2] == ["container", "stop"] for args in client.commands)
    assert any(args[:2] == ["container", "kill"] for args in client.commands)
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


@pytest.mark.parametrize(
    "failure",
    (
        "exception",
        "timed_out",
        "nonzero",
        "stdout_truncated",
        "stderr_truncated",
    ),
)
def test_timeout_kill_result_is_fail_closed_by_forced_cleanup(
    tmp_path,
    failure,
):
    class FaultClient(FakeDockerClient):
        def command(self, args, **kwargs):
            args = list(args)
            if args[:2] == ["container", "kill"]:
                self.commands.append(args)
                if failure == "exception":
                    raise DockerSandboxError("docker_daemon_unavailable")
                return _result(
                    exit_code=1 if failure == "nonzero" else 0,
                    timed_out=failure == "timed_out",
                    stdout_truncated=failure == "stdout_truncated",
                    stderr_truncated=failure == "stderr_truncated",
                )
            return super().command(args, **kwargs)

    client = FaultClient(timeout=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert any(args[:2] == ["container", "kill"] for args in client.commands)
    assert outcome.sandbox_outcome == "timeout"
    assert outcome.cleanup_status == "completed"
    assert outcome.residue_detected is False
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


def test_keyboard_interrupt_cleans_container_and_preserves_verified_outcome(
    tmp_path,
):
    primary = KeyboardInterrupt("stop")

    class InterruptClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[:3] == ["container", "start", "--attach"]:
                self.commands.append(list(args))
                self.started = True
                raise primary
            return super().command(args, **kwargs)

    client = InterruptClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        runner.execute(session, plan)

    assert caught.value is primary
    outcome = caught.value.docker_sandbox_outcome
    assert outcome.sandbox_outcome == "interrupted"
    assert outcome.error_code == "sandbox_interrupted"
    assert outcome.runner_executed is True
    assert outcome.target_started is True
    assert outcome.cleanup_status == "completed"
    assert outcome.residue_detected is False
    assert client.exists is False
    assert store.inspect(session.state_root).state == "ready"


def test_keyboard_interrupt_preserves_primary_when_terminal_inspect_fails(
    tmp_path,
):
    primary = KeyboardInterrupt("stop")

    class InterruptClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[:3] == ["container", "start", "--attach"]:
                self.commands.append(list(args))
                self.started = True
                raise primary
            return super().command(args, **kwargs)

    client = InterruptClient(terminal_inspect_error=True)
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        runner.execute(session, plan)

    assert caught.value is primary
    outcome = caught.value.docker_sandbox_outcome
    assert outcome.sandbox_outcome == "interrupted"
    assert outcome.error_code == "sandbox_interrupted"
    assert outcome.runner_executed is True
    assert outcome.target_started is False
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert client.exists is True
    assert store.inspect(session.state_root).state == "review_required"


def test_cleanup_failure_preserves_full_id_for_review(tmp_path):
    client = FakeDockerClient(cleanup_failure=True)
    _source, store, session, runner, plan, _client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)
    reviewed = store.inspect(session.state_root)

    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert reviewed.state == "review_required"
    assert reviewed.manifest["active_call"]["container_id"] == CONTAINER_ID


@pytest.mark.parametrize(
    "failure",
    (
        "exception",
        "timed_out",
        "nonzero",
        "stdout_truncated",
        "stderr_truncated",
    ),
)
def test_rm_result_requires_exact_success_and_absence_proof(tmp_path, failure):
    class FaultClient(FakeDockerClient):
        def command(self, args, **kwargs):
            args = list(args)
            if args[:3] == ["container", "rm", "--force"]:
                self.commands.append(args)
                if failure == "exception":
                    raise DockerSandboxError("docker_daemon_unavailable")
                return _result(
                    exit_code=1 if failure == "nonzero" else 0,
                    timed_out=failure == "timed_out",
                    stdout_truncated=failure == "stdout_truncated",
                    stderr_truncated=failure == "stderr_truncated",
                )
            return super().command(args, **kwargs)

    client = FaultClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert client.exists is True
    reviewed = store.inspect(session.state_root)
    assert reviewed.state == "review_required"
    assert reviewed.manifest["active_call"]["container_id"] == CONTAINER_ID


@pytest.mark.parametrize(
    "failure",
    (
        "exception",
        "timed_out",
        "nonzero",
        "stdout_truncated",
        "stderr_truncated",
        "whitespace",
    ),
)
def test_rm_success_with_uncertain_absence_requires_review(tmp_path, failure):
    class FaultClient(FakeDockerClient):
        def command(self, args, **kwargs):
            args = list(args)
            if args[:3] == ["container", "ls", "--all"] and not self.exists:
                self.commands.append(args)
                if failure == "exception":
                    raise DockerSandboxError("docker_daemon_unavailable")
                return _result(
                    stdout=b"\n" if failure == "whitespace" else b"",
                    exit_code=1 if failure == "nonzero" else 0,
                    timed_out=failure == "timed_out",
                    stdout_truncated=failure == "stdout_truncated",
                    stderr_truncated=failure == "stderr_truncated",
                )
            return super().command(args, **kwargs)

    client = FaultClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert client.exists is False
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    reviewed = store.inspect(session.state_root)
    assert reviewed.state == "review_required"
    assert reviewed.manifest["active_call"]["container_id"] == CONTAINER_ID


@pytest.mark.parametrize("stream", ("stdout_truncated", "stderr_truncated"))
def test_cleanup_requires_untruncated_absence_proof(tmp_path, stream):
    class TruncatedAbsenceClient(FakeDockerClient):
        def command(self, args, **kwargs):
            result = super().command(args, **kwargs)
            if list(args)[:3] == ["container", "ls", "--all"] and not self.exists:
                return replace(result, **{stream: True})
            return result

    client = TruncatedAbsenceClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    reviewed = store.inspect(session.state_root)
    assert reviewed.state == "review_required"
    assert reviewed.manifest["active_call"]["container_id"] == CONTAINER_ID


def test_workspace_watchdog_stops_call_on_unprovable_scan(tmp_path):
    client = FakeDockerClient(start_delay=0.08)
    calls = 0

    def probe(_workspace):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise DockerSandboxError("sandbox_workspace_limit_exceeded")
        return {"entries": 1, "logical_bytes": 1, "allocated_bytes": 512}

    _source, _store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
        workspace_probe=probe,
        watchdog_interval=0.01,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "container_runtime_failed"
    assert outcome.error_code == "sandbox_workspace_limit_exceeded"
    assert client.stopped is True
    assert outcome.cleanup_status == "completed"


def test_watchdog_interval_is_adaptive_and_bounded():
    assert docker_module._next_watchdog_interval(0) == 0.25
    assert docker_module._next_watchdog_interval(0.008) == 0.25
    assert docker_module._next_watchdog_interval(0.05) == 0.5
    assert docker_module._next_watchdog_interval(0.3) == 2.0


def test_fast_command_cannot_bypass_final_workspace_scan(tmp_path):
    class FastSpecialEntryClient(FakeDockerClient):
        def command(self, args, **kwargs):
            if list(args)[:3] == ["container", "start", "--attach"]:
                workspace = Path(self.plan.workspace)
                (workspace / "late-link").symlink_to("README.md")
            return super().command(args, **kwargs)

    client = FastSpecialEntryClient()
    _source, _store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )

    outcome = runner.execute(session, plan)

    assert outcome.sandbox_outcome == "container_runtime_failed"
    assert outcome.error_code == "sandbox_workspace_limit_exceeded"
    assert outcome.cleanup_status == "completed"
    assert client.exists is False


def test_final_workspace_scan_runs_before_cleanup(tmp_path, monkeypatch):
    events = []
    calls = 0

    def probe(_workspace):
        nonlocal calls
        calls += 1
        events.append(f"probe-{calls}")
        if calls == 2:
            raise DockerSandboxError("sandbox_workspace_limit_exceeded")
        return {"entries": 1, "logical_bytes": 1, "allocated_bytes": 512}

    _source, _store, session, runner, plan, _client = _runner(
        tmp_path,
        workspace_probe=probe,
    )
    original_cleanup = runner._cleanup

    def tracked_cleanup(*args, **kwargs):
        events.append("cleanup")
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(runner, "_cleanup", tracked_cleanup)

    outcome = runner.execute(session, plan)

    assert events[:3] == ["probe-1", "probe-2", "cleanup"]
    assert outcome.error_code == "sandbox_workspace_limit_exceeded"
    assert outcome.cleanup_status == "completed"


def test_live_watchdog_blocks_cleanup_and_requires_review(tmp_path, monkeypatch):
    client = FakeDockerClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    monkeypatch.setattr(runner, "_join_watchdog", lambda _thread, _stop: False)

    outcome = runner.execute(session, plan)

    assert outcome.error_code == "sandbox_workspace_limit_exceeded"
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert store.inspect(session.state_root).state == "review_required"


def test_watchdog_join_race_never_starts_cleanup(tmp_path, monkeypatch):
    client = FakeDockerClient()
    _source, store, session, runner, plan, client = _runner(
        tmp_path,
        client=client,
    )
    monkeypatch.setattr(runner, "_join_watchdog", lambda _thread, _stop: False)
    cleanup_calls = []
    monkeypatch.setattr(
        runner,
        "_cleanup",
        lambda *_args: cleanup_calls.append(True) or True,
    )

    outcome = runner.execute(session, plan)

    assert cleanup_calls == []
    assert outcome.cleanup_status == "failed"
    assert outcome.residue_detected is True
    assert store.inspect(session.state_root).state == "review_required"


def test_measure_workspace_rejects_special_entries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "ok").write_text("data", encoding="utf-8")
    measure_workspace(root)
    (root / "link").symlink_to("ok")

    with pytest.raises(DockerSandboxError, match="sandbox_workspace_limit_exceeded"):
        measure_workspace(root)


def test_cli_socket_and_empty_config_are_identity_bound(tmp_path):
    executable = tmp_path / "docker"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_bytes(b"{}\n")
    endpoint = Path("/tmp") / f"pico-docker-{os.getpid()}-{id(tmp_path)}.sock"
    endpoint.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    try:
        client = DockerClient(executable, endpoint, config)
        first = client.identity_digest()
        executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

        assert first.startswith("sha256:")
        with pytest.raises(DockerSandboxError, match="docker_cli_unavailable"):
            docker_module.verify_docker_cli(client.cli)
    finally:
        listener.close()
        endpoint.unlink(missing_ok=True)


def test_bounded_process_drains_but_retains_only_limit():
    result = docker_module._run_bounded_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"],
        env={"PATH": os.environ.get("PATH", "")},
        timeout=5,
        max_bytes=100,
    )

    assert result.exit_code == 0
    assert result.stdout_bytes == 10000
    assert len(result.stdout) == 100
    assert result.stdout_truncated is True


def test_bounded_process_can_terminate_on_overflow():
    result = docker_module._run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('x' * 10000); sys.stdout.flush(); time.sleep(10)",
        ],
        env={"PATH": os.environ.get("PATH", "")},
        timeout=5,
        max_bytes=100,
        terminate_on_overflow=True,
    )

    assert result.stdout_truncated is True
    assert result.stdout_bytes > 100
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "remote",
    (
        {"DOCKER_HOST": "tcp://example.invalid:2375"},
        {"DOCKER_CONTEXT": "remote"},
    ),
)
def test_discover_local_docker_rejects_remote_environment_before_lookup(
    tmp_path,
    remote,
):
    with pytest.raises(
        DockerSandboxError,
        match="docker_remote_endpoint_unsupported",
    ):
        discover_local_docker(
            environ={**remote, "PATH": ""},
            home=tmp_path,
            host_system="Darwin",
        )


def test_discover_local_docker_binds_trusted_cli_and_desktop_socket(tmp_path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "trusted-docker"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    (binary_dir / "docker").symlink_to(executable)
    short_home = Path("/tmp") / f"pico-docker-discovery-{os.getpid()}-{time.time_ns()}"
    socket_dir = short_home / ".docker" / "run"
    socket_dir.mkdir(parents=True)
    endpoint = socket_dir / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    try:
        cli, discovered = discover_local_docker(
            environ={"PATH": str(binary_dir)},
            home=short_home,
            host_system="Darwin",
        )
    finally:
        listener.close()
        shutil.rmtree(short_home)

    assert cli == binary_dir / "docker"
    assert discovered == endpoint


def test_runtime_docker_config_is_exact_owner_only_and_idempotent(tmp_path):
    root = ensure_runtime_docker_config(tmp_path / "config")
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "config.json").stat().st_mode & 0o777 == 0o600
    assert (root / "config.json").read_bytes() == b"{}\n"
    assert ensure_runtime_docker_config(root) == root
    docker_module._docker_config_identity(root)


def test_prepare_returns_exact_ready_status_without_pull():
    image = load_image_manifest(default_image_manifest_path())

    class ReadyClient:
        commands = []

        def status(self, _image):
            return {
                "status": "ready",
                "reason_code": "ready",
                "image": {
                    "present": True,
                    "digest_match": True,
                    "platform_match": True,
                },
                "network_performed": False,
                "mutation_performed": False,
            }

        def command(self, args, **_kwargs):
            self.commands.append(args)
            raise AssertionError("ready prepare must not pull")

    client = ReadyClient()
    status = DockerClient.prepare(client, image)

    assert status["status"] == "ready"
    assert status["network_performed"] is False
    assert status["mutation_performed"] is False
    assert client.commands == []


@pytest.mark.parametrize(
    ("present", "registry_reference", "reason"),
    (
        (True, "registry.example/pico@", "sandbox_image_identity_mismatch"),
        (False, "", "sandbox_image_missing"),
    ),
)
def test_prepare_fails_closed_without_pull(present, registry_reference, reason):
    image = load_image_manifest(default_image_manifest_path())
    if registry_reference:
        image = replace(
            image,
            registry_reference=registry_reference + image.reference,
        )

    class NotReadyClient:
        commands = []

        def status(self, _image):
            return {
                "status": "not_ready",
                "reason_code": reason,
                "image": {
                    "present": present,
                    "digest_match": False,
                    "platform_match": True,
                },
            }

        def command(self, args, **_kwargs):
            self.commands.append(args)
            raise AssertionError("fail-closed prepare must not pull")

    client = NotReadyClient()

    with pytest.raises(DockerSandboxError, match=reason):
        DockerClient.prepare(client, image)
    assert client.commands == []
