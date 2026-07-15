#!/usr/bin/env python3
"""Probe whether a fixed Sandbox Runtime can satisfy Pico's release contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


NODE_CANDIDATE_VERSION = "24.18.0"
SRT_CANDIDATE_VERSION = "0.0.65"
SRT_CANDIDATE_INTEGRITY = (
    "sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77s"
    "MAI3Q9jP2h973hw5tg6dfEjyayc85rXixuAg=="
)
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_SETTINGS_PATHS = 4096
SRT_SOURCE_REJECTIONS = {
    "0.0.65": {
        "check_id": "future_sensitive_workspace_write",
        "evidence_id": "srt_0_0_65_linux_deny_write_glob_skipped",
        "source_revision": (
            "npm-sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77s"
            "MAI3Q9jP2h973hw5tg6dfEjyayc85rXixuAg=="
        ),
    }
}
IMPLEMENTED_MANDATORY_CHECK_IDS = (
    "settings_schema",
    "workspace_read",
    "workspace_write",
    "workspace_sibling_read",
    "ordinary_home_read",
    "sensitive_workspace_read",
    "sensitive_workspace_write",
    "future_sensitive_workspace_write",
    "git_metadata_write",
    "external_write",
    "external_tcp",
    "external_udp",
    "dns_resolution",
    "localhost_ipv4",
    "localhost_ipv6",
    "listener_ipv4",
    "listener_ipv6",
    "unix_socket_connect",
    "unix_socket_create",
    "child_process_inheritance",
    "grandchild_process_inheritance",
    "linux_proc_host_env",
    "linux_dev_shm",
    "timeout_cleanup",
    "detached_setsid_cleanup",
    "argv_fidelity",
    "target_nonzero",
    "wrapper_bootstrap",
    "wrapper_cleanup",
)
PENDING_MANDATORY_CHECK_IDS = (
    "platform_provenance",
    "workspace_first_read_frontier",
    "credential_read",
    "user_memory_read",
    "symlink_escape_read",
    "ordinary_home_write",
    "toolchain_write",
    "external_git_metadata_write",
    "user_notes_write",
    "future_nested_protected_write",
    "macos_apple_events",
    "macos_keychain",
    "macos_clipboard",
    "host_fallback_trap",
    "host_fd_inheritance",
    "target_not_started",
    "sigint_cleanup",
    "sigterm_cleanup",
    "detached_double_fork_cleanup",
    "helper_residue",
)
MANDATORY_CHECK_IDS = IMPLEMENTED_MANDATORY_CHECK_IDS + PENDING_MANDATORY_CHECK_IDS
_EXECUTION_RECORDS = ContextVar("srt_feasibility_execution_records", default=None)


def _resolved_executable(path):
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        found = shutil.which(str(candidate))
        if found is None:
            return None
        candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def _base_report(*, platform_name, architecture, real):
    return {
        "record_type": "srt_feasibility",
        "format_version": 1,
        "platform": platform_name,
        "architecture": architecture,
        "mode": "real" if real else "offline",
        "status": "not_ready",
        "reason_code": "probe_not_run",
        "candidate": {
            "node_version": NODE_CANDIDATE_VERSION,
            "srt_package": "@anthropic-ai/sandbox-runtime",
            "srt_version": SRT_CANDIDATE_VERSION,
            "srt_integrity": SRT_CANDIDATE_INTEGRITY,
        },
        "harness": _harness_identity(),
        "versions": {
            "node_candidate": NODE_CANDIDATE_VERSION,
            "srt_candidate": SRT_CANDIDATE_VERSION,
        },
        "checks": [],
        "mandatory_passed": 0,
        "mandatory_failed": 0,
        "host_fallback_count": None,
    }


def _harness_identity():
    repo = Path(__file__).resolve().parents[1]
    paths = (
        repo / "scripts" / "srt_feasibility.py",
        repo / "scripts" / "aggregate_srt_feasibility.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(repo).as_posix().encode("utf-8") + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            return {"commit": "", "digest": "", "dirty": True}
    git = _resolved_executable("git")
    if git is None:
        return {
            "commit": "",
            "digest": "sha256:" + digest.hexdigest(),
            "dirty": True,
        }
    env = {
        "PATH": str(git.parent),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    relative_paths = [path.relative_to(repo).as_posix() for path in paths]
    try:
        commit_result = subprocess.run(
            [str(git), "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        tracked_result = subprocess.run(
            [str(git), "ls-files", "--error-unmatch", "--", *relative_paths],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty_result = subprocess.run(
            [str(git), "diff", "--quiet", "HEAD", "--", *relative_paths],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "commit": "",
            "digest": "sha256:" + digest.hexdigest(),
            "dirty": True,
        }
    commit = commit_result.stdout.strip()
    clean = (
        commit_result.returncode == 0
        and len(commit) == 40
        and all(char in "0123456789abcdef" for char in commit)
        and tracked_result.returncode == 0
        and dirty_result.returncode == 0
    )
    return {
        "commit": commit if clean else "",
        "digest": "sha256:" + digest.hexdigest(),
        "dirty": not clean,
    }


def build_report(*, platform_name, architecture, srt_path, node_path, real):
    report = _base_report(
        platform_name=platform_name,
        architecture=architecture,
        real=real,
    )
    rejection = SRT_SOURCE_REJECTIONS.get(SRT_CANDIDATE_VERSION)
    if rejection is not None:
        report["status"] = "failed"
        report["reason_code"] = "candidate_rejected"
        report["versions"]["srt_source_revision"] = rejection["source_revision"]
        report["checks"] = [
            _check(rejection["check_id"], False, rejection["evidence_id"])
        ]
        report["mandatory_failed"] = 1
        return report

    srt = _resolved_executable(srt_path)
    node = _resolved_executable(node_path)
    if srt is None:
        report["reason_code"] = "srt_unavailable"
        return report
    if real and node is None:
        report["reason_code"] = "node_unavailable"
        return report
    report["status"] = "ready"
    report["reason_code"] = "ready_for_probe"
    return report


def _check(check_id, passed, reason_code):
    return {
        "check_id": check_id,
        "mandatory": True,
        "status": "pass" if passed else "fail",
        "reason_code": reason_code,
    }


def _not_ready(check_id, reason_code):
    return {
        "check_id": check_id,
        "mandatory": True,
        "status": "not_ready",
        "reason_code": reason_code,
    }


def _validate_settings_payload(payload):
    expected_top = {
        "network",
        "filesystem",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
        "allowAppleEvents",
    }
    expected_network = {
        "allowedDomains",
        "deniedDomains",
        "strictAllowlist",
        "allowLocalBinding",
        "allowUnixSockets",
        "allowAllUnixSockets",
    }
    expected_filesystem = {"denyRead", "allowRead", "allowWrite", "denyWrite"}
    if not isinstance(payload, dict) or set(payload) != expected_top:
        raise ValueError("invalid settings schema")
    network = payload.get("network")
    filesystem = payload.get("filesystem")
    if (
        not isinstance(network, dict)
        or set(network) != expected_network
        or not isinstance(filesystem, dict)
        or set(filesystem) != expected_filesystem
    ):
        raise ValueError("invalid settings schema")
    if not all(
        isinstance(network[name], list)
        and all(isinstance(value, str) for value in network[name])
        for name in ("allowedDomains", "deniedDomains", "allowUnixSockets")
    ):
        raise ValueError("invalid settings schema")
    if sum(len(filesystem[name]) for name in expected_filesystem) > MAX_SETTINGS_PATHS:
        raise ValueError("invalid settings schema")
    if not all(
        type(network[name]) is bool
        for name in ("strictAllowlist", "allowLocalBinding", "allowAllUnixSockets")
    ):
        raise ValueError("invalid settings schema")
    if not all(
        isinstance(filesystem[name], list)
        and all(
            isinstance(value, str)
            and "\x00" not in value
            and Path(value).is_absolute()
            for value in filesystem[name]
        )
        for name in expected_filesystem
    ):
        raise ValueError("invalid settings schema")
    if not all(
        type(payload[name]) is bool
        for name in (
            "enableWeakerNestedSandbox",
            "enableWeakerNetworkIsolation",
            "allowAppleEvents",
        )
    ):
        raise ValueError("invalid settings schema")
    if (
        network["allowedDomains"] != []
        or network["deniedDomains"] != ["*"]
        or network["strictAllowlist"] is not True
        or network["allowLocalBinding"] is not False
        or network["allowUnixSockets"] != []
        or network["allowAllUnixSockets"] is not False
        or filesystem["allowRead"] != []
        or payload["enableWeakerNestedSandbox"] is not False
        or payload["enableWeakerNetworkIsolation"] is not False
        or payload["allowAppleEvents"] is not False
    ):
        raise ValueError("invalid settings schema")
    return payload


def _load_settings_payload(path):
    raw = path.read_bytes()
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError("invalid settings schema")

    def exact_object(pairs):
        payload = {}
        for name, value in pairs:
            if name in payload:
                raise ValueError("invalid settings schema")
            payload[name] = value
        return payload

    return _validate_settings_payload(json.loads(raw, object_pairs_hook=exact_object))


def _settings(root, *, unknown=False):
    payload = {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "strictAllowlist": True,
            "allowLocalBinding": False,
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
        },
        "filesystem": {
            "denyRead": [
                str(root / "workspace" / ".env"),
                str(root / "workspace" / ".env.*"),
                str(root / "workspace" / ".pico"),
                str(root / "sibling"),
                str(root / "home"),
            ],
            "allowRead": [],
            "allowWrite": [str(root / "workspace"), str(root / "call")],
            "denyWrite": [
                str(root / "workspace" / ".env"),
                str(root / "workspace" / ".env.*"),
                str(root / "workspace" / ".pico"),
                str(root / "workspace" / ".git"),
                str(root / "workspace" / "notes"),
                str(root / "workspace" / "agent_notes.md"),
                str(root / "external"),
                str(root / "home"),
            ],
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }
    if unknown:
        payload["unknownPicoProbeKey"] = True
    path = root / ("invalid-settings.json" if unknown else "settings.json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(argv, *, cwd, timeout=10):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, False
    except subprocess.TimeoutExpired:
        return None, True


@contextmanager
def _capture_execution_records():
    records = []
    token = _EXECUTION_RECORDS.set(records)
    try:
        yield records
    finally:
        _EXECUTION_RECORDS.reset(token)


def _record_execution(isolation_implementation):
    records = _EXECUTION_RECORDS.get()
    if records is not None:
        records.append(str(isolation_implementation))


def _host_fallback_count(records):
    if not records:
        return None
    return sum(item != "srt" for item in records)


def _sandbox_command(srt, settings, command, *, cwd, timeout=10):
    _record_execution("srt")
    launcher = list(srt) if isinstance(srt, (tuple, list)) else [str(srt)]
    return _run(
        [*map(str, launcher), "--settings", str(settings), "--", *command],
        cwd=cwd,
        timeout=timeout,
    )


def _verified_launcher(node, srt):
    """Return the production-shaped launcher after exact version checks."""
    try:
        node_result = subprocess.run(
            [str(node), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "node_version_unavailable", "", ""
    node_version = (node_result.stdout or node_result.stderr or "").strip().removeprefix("v")
    if node_result.returncode != 0 or node_version != NODE_CANDIDATE_VERSION:
        return None, "node_version_mismatch", node_version, ""

    srt_version = ""
    for parent in (srt.parent, *tuple(srt.parents)[:4]):
        package = parent / "package.json"
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("name") == "@anthropic-ai/sandbox-runtime":
            srt_version = str(payload.get("version", ""))
            break
    if not srt_version and os.access(srt, os.X_OK):
        try:
            result = subprocess.run(
                [str(srt), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            output = f"{result.stdout or ''} {result.stderr or ''}"
            if SRT_CANDIDATE_VERSION in output:
                srt_version = SRT_CANDIDATE_VERSION
    if srt_version != SRT_CANDIDATE_VERSION:
        return None, "srt_version_mismatch", node_version, srt_version
    launcher = (node, srt) if srt.suffix in {".js", ".cjs", ".mjs"} or not os.access(srt, os.X_OK) else (srt,)
    return launcher, "versions_verified", node_version, srt_version


def _probe_settings_schema(root):
    try:
        payload = _load_settings_payload(_settings(root))
    except (OSError, ValueError, json.JSONDecodeError):
        return _check("settings_schema", False, "pico_valid_settings_rejected")
    payload["unknownPicoProbeKey"] = True
    try:
        _validate_settings_payload(payload)
    except ValueError:
        return _check("settings_schema", True, "pico_exact_schema_verified")
    return _check("settings_schema", False, "pico_unknown_key_accepted")


def _blocked(code, timed_out):
    return not timed_out and code not in (None, 0)


def _probe_filesystem(srt, settings, root):
    workspace = root / "workspace"
    checks = []
    code, timed_out = _sandbox_command(
        srt, settings, ["/bin/sh", "-c", "test -r README.md"], cwd=workspace
    )
    read_ok = code == 0 and not timed_out
    checks.append(
        _check("workspace_read", read_ok, "read_allowed" if read_ok else "read_failed")
    )

    code, timed_out = _sandbox_command(
        srt,
        settings,
        [
            "/bin/sh",
            "-c",
            (
                "printf ok > normal.tmp && mv normal.tmp normal.txt && "
                "test \"$(cat normal.txt)\" = ok && rm normal.txt"
            ),
        ],
        cwd=workspace,
    )
    write_ok = code == 0 and not timed_out and not (workspace / "normal.txt").exists()
    checks.append(
        _check("workspace_write", write_ok, "write_allowed" if write_ok else "write_failed")
    )

    for check_id, path in (
        ("workspace_sibling_read", root / "sibling" / "private.txt"),
        ("ordinary_home_read", root / "home" / "private.txt"),
    ):
        code, timed_out = _sandbox_command(
            srt,
            settings,
            ["/bin/sh", "-c", 'cat "$1" >/dev/null', "probe", str(path)],
            cwd=workspace,
        )
        denied = _blocked(code, timed_out)
        checks.append(
            _check(check_id, denied, "read_denied" if denied else "read_allowed")
        )

    code, timed_out = _sandbox_command(
        srt,
        settings,
        [
            "/bin/sh",
            "-c",
            "cat .env >/dev/null 2>&1 || cat .pico/state >/dev/null 2>&1",
        ],
        cwd=workspace,
    )
    denied = _blocked(code, timed_out)
    checks.append(
        _check(
            "sensitive_workspace_read",
            denied,
            "read_denied" if denied else "read_allowed",
        )
    )

    env_file = workspace / ".env"
    original_env = env_file.read_text(encoding="utf-8")
    original = (workspace / ".pico" / "state").read_text()
    code, timed_out = _sandbox_command(
        srt,
        settings,
        [
            "/bin/sh",
            "-c",
            "printf changed > .env; printf changed > .pico/state",
        ],
        cwd=workspace,
    )
    denied = (
        _blocked(code, timed_out)
        and env_file.read_text(encoding="utf-8") == original_env
        and (workspace / ".pico" / "state").read_text() == original
    )
    checks.append(
        _check(
            "sensitive_workspace_write",
            denied,
            "write_denied" if denied else "write_allowed",
        )
    )

    future_env = workspace / ".env.random"
    code, timed_out = _sandbox_command(
        srt,
        settings,
        ["/bin/sh", "-c", "printf changed > .env.random"],
        cwd=workspace,
    )
    future_denied = _blocked(code, timed_out) and not future_env.exists()
    checks.append(
        _check(
            "future_sensitive_workspace_write",
            future_denied,
            "future_write_denied" if future_denied else "future_write_allowed",
        )
    )

    git_config = workspace / ".git" / "config"
    original_git = git_config.read_text(encoding="utf-8")
    code, timed_out = _sandbox_command(
        srt, settings, ["/bin/sh", "-c", "printf changed > .git/config"], cwd=workspace
    )
    git_denied = (
        _blocked(code, timed_out)
        and git_config.read_text(encoding="utf-8") == original_git
    )
    checks.append(
        _check(
            "git_metadata_write",
            git_denied,
            "write_denied" if git_denied else "write_allowed",
        )
    )

    external = root / "external" / "state"
    original_external = external.read_text(encoding="utf-8")
    code, timed_out = _sandbox_command(
        srt,
        settings,
        ["/bin/sh", "-c", 'printf changed > "$1"', "probe", str(external)],
        cwd=workspace,
    )
    external_denied = (
        _blocked(code, timed_out)
        and external.read_text(encoding="utf-8") == original_external
    )
    checks.append(
        _check(
            "external_write",
            external_denied,
            "write_denied" if external_denied else "write_allowed",
        )
    )
    return checks


def _node_denial_probe(srt, settings, node, root, check_id, source):
    code, timed_out = _run(
        [str(node), "-e", source, "allow"],
        cwd=root / "workspace",
        timeout=8,
    )
    if code != 0 or timed_out:
        return _not_ready(check_id, "host_positive_control_failed")
    code, timed_out = _sandbox_command(
        srt,
        settings,
        [str(node), "-e", source, "deny"],
        cwd=root / "workspace",
        timeout=8,
    )
    passed = code == 0 and not timed_out
    return _check(
        check_id,
        passed,
        "blocked_after_host_positive_control" if passed else "not_blocked",
    )


_NODE_EXPECTATION = (
    "const expected=process.argv[1];let settled=false;"
    "function finish(allowed){if(settled)return;settled=true;"
    "process.exit(allowed===(expected==='allow')?0:1)};"
)


@contextmanager
def _tcp_listener(family, host):
    server = socket.socket(family, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, 0))
        server.listen()
        yield server.getsockname()[1]
    finally:
        server.close()


@contextmanager
def _unix_listener(path):
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen()
        yield
    finally:
        server.close()
        path.unlink(missing_ok=True)


def _connect_source(host, port):
    return (
        _NODE_EXPECTATION
        + "const net=require('net');"
        + f"const s=net.connect({{host:{json.dumps(host)},port:{port}}});"
        + "s.setTimeout(1500,()=>{s.destroy();finish(false)});"
        + "s.once('connect',()=>{s.destroy();finish(true)});"
        + "s.once('error',()=>finish(false));"
    )


def _listener_source(host):
    return (
        _NODE_EXPECTATION
        + "const net=require('net');const s=net.createServer();"
        + "s.once('error',()=>finish(false));"
        + f"s.listen(0,{json.dumps(host)},()=>s.close(()=>finish(true)));"
        + "setTimeout(()=>{s.close();finish(false)},1500);"
    )


def _probe_network(srt, settings, node, root):
    checks = []
    checks.append(
        _node_denial_probe(
            srt,
            settings,
            node,
            root,
            "external_tcp",
            _connect_source("1.1.1.1", 443),
        )
    )
    external_udp = (
        _NODE_EXPECTATION
        + "const d=require('dgram').createSocket('udp4');"
        + "const q=Buffer.from('123401000001000000000000076578616d706c6503636f6d0000010001','hex');"
        + "d.once('message',()=>{d.close();finish(true)});"
        + "d.once('error',()=>{d.close();finish(false)});"
        + "d.send(q,53,'1.1.1.1',e=>{if(e){d.close();finish(false)}});"
        + "setTimeout(()=>{d.close();finish(false)},1500);"
    )
    checks.append(
        _node_denial_probe(
            srt, settings, node, root, "external_udp", external_udp
        )
    )
    dns = (
        _NODE_EXPECTATION
        + "require('dns').lookup('example.com',e=>finish(!e));"
        + "setTimeout(()=>finish(false),1500);"
    )
    checks.append(
        _node_denial_probe(srt, settings, node, root, "dns_resolution", dns)
    )

    for check_id, family, host in (
        ("localhost_ipv4", socket.AF_INET, "127.0.0.1"),
        ("localhost_ipv6", socket.AF_INET6, "::1"),
    ):
        try:
            with _tcp_listener(family, host) as port:
                checks.append(
                    _node_denial_probe(
                        srt,
                        settings,
                        node,
                        root,
                        check_id,
                        _connect_source(host, port),
                    )
                )
        except OSError:
            checks.append(_not_ready(check_id, "host_positive_control_unavailable"))

    checks.extend(
        (
            _node_denial_probe(
                srt,
                settings,
                node,
                root,
                "listener_ipv4",
                _listener_source("127.0.0.1"),
            ),
            _node_denial_probe(
                srt,
                settings,
                node,
                root,
                "listener_ipv6",
                _listener_source("::1"),
            ),
        )
    )

    unix_connect_path = root / "call" / "connect.sock"
    unix_connect = (
        _NODE_EXPECTATION
        + "const net=require('net');"
        + f"const s=net.connect({json.dumps(str(unix_connect_path))});"
        + "s.setTimeout(1500,()=>{s.destroy();finish(false)});"
        + "s.once('connect',()=>{s.destroy();finish(true)});"
        + "s.once('error',()=>finish(false));"
    )
    try:
        with _unix_listener(unix_connect_path):
            checks.append(
                _node_denial_probe(
                    srt,
                    settings,
                    node,
                    root,
                    "unix_socket_connect",
                    unix_connect,
                )
            )
    except OSError:
        checks.append(
            _not_ready("unix_socket_connect", "host_positive_control_unavailable")
        )

    unix_create_path = root / "call" / "create.sock"
    unix_create = (
        _NODE_EXPECTATION
        + "const fs=require('fs'),net=require('net');"
        + f"const p={json.dumps(str(unix_create_path))};"
        + "try{fs.unlinkSync(p)}catch{}const s=net.createServer();"
        + "s.once('error',()=>finish(false));"
        + "s.listen(p,()=>s.close(()=>{try{fs.unlinkSync(p)}catch{}finish(true)}));"
        + "setTimeout(()=>{s.close();try{fs.unlinkSync(p)}catch{}finish(false)},1500);"
    )
    checks.append(
        _node_denial_probe(
            srt,
            settings,
            node,
            root,
            "unix_socket_create",
            unix_create,
        )
    )
    return checks


def _inheritance_operation(root, marker, port):
    return (
        "const fs=require('fs'),net=require('net');let violated=false,settled=false;"
        f"try{{fs.readFileSync({json.dumps(str(root / 'sibling' / 'private.txt'))});"
        "violated=true}catch{}"
        f"try{{fs.writeFileSync({json.dumps(str(root / 'external' / 'state'))},'changed');"
        "violated=true}catch{}"
        f"try{{fs.writeFileSync({json.dumps(str(root / 'workspace' / ('.env.' + marker)))},'changed');"
        "violated=true}catch{}"
        "function finish(networkAllowed){if(settled)return;settled=true;"
        "if(violated||networkAllowed)process.exit(1);"
        f"fs.writeFileSync({json.dumps(str(root / 'workspace' / (marker + '.ok')))},'ok');"
        "process.exit(0)}"
        f"const s=net.connect({{host:'127.0.0.1',port:{port}}});"
        "s.setTimeout(1500,()=>{s.destroy();finish(false)});"
        "s.once('connect',()=>{s.destroy();finish(true)});"
        "s.once('error',()=>finish(false));"
    )


def _probe_processes(srt, settings, node, root):
    workspace = root / "workspace"
    spawn_source = (
        "const c=require('child_process');"
        "const r=c.spawnSync(process.execPath,['-e',process.argv[1],"
        "...process.argv.slice(2)],{stdio:'ignore'});"
        "process.exit(r.status===0?0:1);"
    )
    try:
        with _tcp_listener(socket.AF_INET, "127.0.0.1") as port:
            host_code, host_timeout = _run(
                [str(node), "-e", _connect_source("127.0.0.1", port), "allow"],
                cwd=workspace,
                timeout=8,
            )
            if host_code != 0 or host_timeout:
                return [
                    _not_ready(
                        "child_process_inheritance",
                        "host_positive_control_failed",
                    ),
                    _not_ready(
                        "grandchild_process_inheritance",
                        "host_positive_control_failed",
                    ),
                ]
            checks = []
            for check_id, marker, command in (
                (
                    "child_process_inheritance",
                    "child",
                    [str(node), "-e", spawn_source, _inheritance_operation(root, "child", port)],
                ),
                (
                    "grandchild_process_inheritance",
                    "grandchild",
                    [
                        str(node),
                        "-e",
                        spawn_source,
                        spawn_source,
                        _inheritance_operation(root, "grandchild", port),
                    ],
                ),
            ):
                code, timed_out = _sandbox_command(
                    srt, settings, command, cwd=workspace, timeout=10
                )
                marker_path = workspace / f"{marker}.ok"
                passed = (
                    code == 0
                    and not timed_out
                    and marker_path.is_file()
                    and marker_path.read_text(encoding="utf-8") == "ok"
                    and (root / "external" / "state").read_text(encoding="utf-8")
                    == "external\n"
                    and not (workspace / f".env.{marker}").exists()
                )
                checks.append(
                    _check(
                        check_id,
                        passed,
                        "critical_denies_inherited" if passed else "deny_not_inherited",
                    )
                )
            return checks
    except OSError:
        return [
            _not_ready(
                "child_process_inheritance", "host_positive_control_unavailable"
            ),
            _not_ready(
                "grandchild_process_inheritance",
                "host_positive_control_unavailable",
            ),
        ]


def _probe_linux_ipc(srt, settings, node, root, platform_name):
    if platform_name != "linux":
        return [
            _check(
                "linux_proc_host_env",
                not Path("/proc").exists(),
                "host_proc_boundary_absent",
            ),
            _check(
                "linux_dev_shm",
                not Path("/dev/shm").exists(),
                "host_shm_boundary_absent",
            ),
        ]

    token = "PICO_SRT_PROC_" + secrets.token_hex(16)
    sleep = _resolved_executable(shutil.which("sleep"))
    if sleep is None:
        proc_check = _not_ready("linux_proc_host_env", "host_positive_control_unavailable")
    else:
        helper = subprocess.Popen(
            [str(sleep), "10"],
            env=dict(os.environ, PICO_SRT_PROC_SENTINEL=token),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc_source = (
            _NODE_EXPECTATION
            + "const fs=require('fs');let found=false;"
            + "for(const name of fs.readdirSync('/proc')){if(!/^\\d+$/.test(name))continue;"
            + "try{if(fs.readFileSync('/proc/'+name+'/environ').includes("
            + json.dumps(token)
            + ")){found=true;break}}catch{}}finish(found);"
        )
        try:
            proc_check = _node_denial_probe(
                srt,
                settings,
                node,
                root,
                "linux_proc_host_env",
                proc_source,
            )
        finally:
            helper.terminate()
            try:
                helper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait(timeout=2)

    shm_root = Path("/dev/shm")
    if not shm_root.is_dir() or not os.access(shm_root, os.W_OK):
        shm_check = _not_ready("linux_dev_shm", "host_positive_control_unavailable")
    else:
        shm_path = shm_root / ("pico-srt-" + secrets.token_hex(16))
        try:
            descriptor = os.open(shm_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b"pico-srt-host-shm")
            finally:
                os.close(descriptor)
            shm_source = (
                _NODE_EXPECTATION
                + "const fs=require('fs');let visible=false;"
                + f"try{{visible=fs.readFileSync({json.dumps(str(shm_path))},'utf8')"
                + "==='pico-srt-host-shm'}catch{}finish(visible);"
            )
            shm_check = _node_denial_probe(
                srt, settings, node, root, "linux_dev_shm", shm_source
            )
        except OSError:
            shm_check = _not_ready(
                "linux_dev_shm", "host_positive_control_unavailable"
            )
        finally:
            shm_path.unlink(missing_ok=True)
    return [proc_check, shm_check]


def _probe_timeout_cleanup(srt, settings, root, platform_name):
    pid_file = root / "call" / "timeout.pid"
    token = "pico-srt-timeout-" + secrets.token_hex(16)
    launcher = list(srt) if isinstance(srt, (tuple, list)) else [str(srt)]
    process = subprocess.Popen(
        [
            *map(str, launcher),
            "--settings",
            str(settings),
            "--",
            "/bin/sh",
            "-c",
            (
                f": {token}; (trap '' TERM; while :; do sleep 1; done) & "
                f"printf $! > {pid_file}; wait"
            ),
        ],
        cwd=root / "workspace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    if platform_name == "linux":
        residue_pids = _linux_token_pids(token)
        alive = bool(residue_pids)
    else:
        residue_pids = [pid] if pid is not None and _pid_alive(pid) else []
        alive = bool(residue_pids)
    for residue_pid in residue_pids:
        try:
            os.kill(residue_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    passed = timed_out and process.poll() is not None and not alive
    return _check("timeout_cleanup", passed, "process_tree_reaped" if passed else "process_residue")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_token_pids(token):
    token_bytes = token.encode()
    matches = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return matches
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if token_bytes in command:
            matches.append(int(entry.name))
    return matches


def _probe_detached_cleanup(srt, settings, node, root, platform_name):
    heartbeat = root / "call" / "detached.heartbeat"
    pid_file = root / "call" / "detached.pid"
    token = "pico-srt-detached-" + secrets.token_hex(16)
    child_source = (
        "const fs=require('fs');"
        "fs.writeFileSync(process.argv[2],String(process.pid));"
        "setInterval(()=>fs.appendFileSync(process.argv[1],'x'),50);"
    )
    parent_source = (
        "const c=require('child_process');"
        "c.spawn(process.execPath,['-e',process.argv[1],...process.argv.slice(2)],"
        "{detached:true,stdio:'ignore'}).unref();setInterval(()=>{},1000);"
    )
    launcher = list(srt) if isinstance(srt, (tuple, list)) else [str(srt)]
    try:
        process = subprocess.Popen(
            [
                *map(str, launcher),
                "--settings",
                str(settings),
                "--",
                str(node),
                "-e",
                parent_source,
                child_source,
                str(heartbeat),
                str(pid_file),
                token,
            ],
            cwd=root / "workspace",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return _check("detached_setsid_cleanup", False, "wrapper_start_failed")

    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if pid_file.exists() and heartbeat.exists() and heartbeat.stat().st_size >= 2:
            break
        if process.poll() is not None:
            break
        time.sleep(0.05)
    started = pid_file.exists() and heartbeat.exists() and heartbeat.stat().st_size >= 2
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)

    before = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.35)
    after = heartbeat.stat().st_size if heartbeat.exists() else 0
    detached_pid = int(pid_file.read_text()) if pid_file.exists() else None
    if platform_name == "linux":
        residue_pids = _linux_token_pids(token)
        residue = bool(residue_pids)
    else:
        residue_pids = [detached_pid] if detached_pid and _pid_alive(detached_pid) else []
        residue = bool(residue_pids)
    passed = started and before == after and not residue
    for pid in residue_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return _check(
        "detached_setsid_cleanup",
        passed,
        "detached_tree_reaped" if passed else "detached_process_residue",
    )


def _probe_argv(srt, settings, root):
    output = root / "workspace" / "argv.json"
    values = ["", "a b", "quote'\"", "!", "line\nbreak", "雪"]
    script = "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2:],ensure_ascii=False))"
    code, timed_out = _sandbox_command(
        srt,
        settings,
        ["/usr/bin/python3", "-c", script, str(output), *values],
        cwd=root / "workspace",
    )
    passed = code == 0 and not timed_out and json.loads(output.read_text()) == values
    return _check("argv_fidelity", passed, "argv_preserved" if passed else "argv_changed")


def _probe_lifecycle(srt, settings, root):
    checks = []
    marker = root / "workspace" / "started"
    code, timed_out = _sandbox_command(
        srt,
        settings,
        ["/bin/sh", "-c", f"printf yes > {marker}; exit 23"],
        cwd=root / "workspace",
    )
    nonzero_ok = code == 23 and not timed_out and marker.exists()
    checks.append(_check("target_nonzero", nonzero_ok, "exit_preserved" if nonzero_ok else "exit_changed"))
    checks.append(_check("wrapper_bootstrap", marker.exists(), "target_started" if marker.exists() else "target_not_started"))

    before = {path.name for path in root.iterdir()}
    code, timed_out = _sandbox_command(
        srt, settings, ["/usr/bin/true"], cwd=root / "workspace"
    )
    after = {path.name for path in root.iterdir()}
    cleanup_ok = code == 0 and not timed_out and before == after
    checks.append(_check("wrapper_cleanup", cleanup_ok, "cleanup_complete" if cleanup_ok else "cleanup_incomplete"))
    return checks


def _pending_mandatory_checks():
    return [
        _not_ready(check_id, "probe_not_implemented")
        for check_id in PENDING_MANDATORY_CHECK_IDS
    ]


def _finalize_probe_status(report, checks):
    if tuple(item["check_id"] for item in checks) != MANDATORY_CHECK_IDS:
        report["status"] = "failed"
        report["reason_code"] = "check_set_incomplete"
    elif report["host_fallback_count"]:
        report["status"] = "failed"
        report["reason_code"] = "host_fallback_detected"
    elif any(item["status"] == "not_ready" for item in checks):
        report["status"] = "not_ready"
        reasons = {
            item["reason_code"]
            for item in checks
            if item["status"] == "not_ready"
        }
        if "probe_not_implemented" in reasons:
            report["reason_code"] = "mandatory_probe_not_implemented"
        elif any(reason.startswith("host_positive_control_") for reason in reasons):
            report["reason_code"] = "host_positive_control_unavailable"
        else:
            report["reason_code"] = "mandatory_check_not_ready"
    elif report["host_fallback_count"] is None:
        report["status"] = "not_ready"
        report["reason_code"] = "host_fallback_evidence_unavailable"
    elif report["mandatory_failed"]:
        report["status"] = "failed"
        report["reason_code"] = "mandatory_check_failed"
    else:
        report["status"] = "passed"
        report["reason_code"] = "mandatory_checks_passed"
    return report


def run_real_probe(*, platform_name, architecture, srt_path, node_path):
    report = build_report(
        platform_name=platform_name,
        architecture=architecture,
        srt_path=srt_path,
        node_path=node_path,
        real=True,
    )
    if report["status"] != "ready":
        return report
    srt = _resolved_executable(srt_path)
    node = _resolved_executable(node_path)
    launcher, version_reason, node_version, srt_version = _verified_launcher(node, srt)
    report["versions"].update(node_actual=node_version, srt_actual=srt_version)
    if launcher is None:
        report["status"] = "not_ready"
        report["reason_code"] = version_reason
        return report
    with tempfile.TemporaryDirectory(prefix="pico-srt-feasibility-") as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        (workspace / ".pico").mkdir(parents=True)
        (workspace / ".git").mkdir()
        (workspace / "notes").mkdir()
        (root / "call").mkdir()
        (root / "sibling").mkdir()
        (root / "home").mkdir()
        (root / "external").mkdir()
        (workspace / "README.md").write_text("probe\n", encoding="utf-8")
        (workspace / ".env").write_text("secret\n", encoding="utf-8")
        (workspace / ".pico" / "state").write_text("private\n", encoding="utf-8")
        (workspace / ".git" / "config").write_text("safe\n", encoding="utf-8")
        (workspace / "agent_notes.md").write_text("notes\n", encoding="utf-8")
        (root / "sibling" / "private.txt").write_text("sibling\n", encoding="utf-8")
        (root / "home" / "private.txt").write_text("home\n", encoding="utf-8")
        (root / "external" / "state").write_text("external\n", encoding="utf-8")
        settings = _settings(root)
        with _capture_execution_records() as execution_records:
            checks = [_probe_settings_schema(root)]
            checks.extend(_probe_filesystem(launcher, settings, root))
            checks.extend(_probe_network(launcher, settings, node, root))
            checks.extend(_probe_processes(launcher, settings, node, root))
            checks.extend(
                _probe_linux_ipc(
                    launcher, settings, node, root, platform_name
                )
            )
            checks.append(
                _probe_timeout_cleanup(
                    launcher, settings, root, platform_name
                )
            )
            checks.append(
                _probe_detached_cleanup(
                    launcher, settings, node, root, platform_name
                )
            )
            checks.append(_probe_argv(launcher, settings, root))
            checks.extend(_probe_lifecycle(launcher, settings, root))
            checks.extend(_pending_mandatory_checks())

    report["checks"] = checks
    report["host_fallback_count"] = _host_fallback_count(execution_records)
    report["mandatory_passed"] = sum(item["status"] == "pass" for item in checks)
    report["mandatory_failed"] = sum(item["status"] != "pass" for item in checks)
    return _finalize_probe_status(report, checks)


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=("current", "macos", "linux"),
        default="current",
        help="Platform contract to probe.",
    )
    parser.add_argument("--architecture", default=platform.machine())
    parser.add_argument("--srt", default=None, help="Explicit SRT launcher path.")
    parser.add_argument("--node", default=None, help="Explicit Node executable path.")
    parser.add_argument(
        "--managed",
        action="store_true",
        help="Use the verified Pico-managed Node and SRT identity.",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Run the real mandatory corpus. Offline mode only reports readiness.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the real probe for stability evidence.")
    return parser


def _platform_name(value):
    if value == "macos":
        return "darwin"
    if value == "linux":
        return "linux"
    return platform.system().casefold()


def _architecture_name(value):
    return {
        "aarch64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }.get(str(value).casefold(), str(value).casefold())


def _render_text(report):
    fallback_count = report["host_fallback_count"]
    fallback_text = (
        str(fallback_count) if type(fallback_count) is int else "not_observed"
    )
    return (
        f"SRT feasibility: {report['status']}\n"
        f"platform: {report['platform']}/{report['architecture']}\n"
        f"mode: {report['mode']}\n"
        f"reason: {report['reason_code']}\n"
        f"mandatory: {report['mandatory_passed']} passed, "
        f"{report['mandatory_failed']} failed\n"
        f"host fallback: {fallback_text}\n"
    )


def _managed_paths():
    from pico.sandbox_toolchain import SandboxToolchain

    root = Path.home().resolve(strict=True) / ".pico" / "toolchains" / "sandbox"
    identity = SandboxToolchain(root, create_root=False).identity()
    return identity.srt_entry_path, identity.node_path


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least one")
    if args.managed and (args.srt is not None or args.node is not None):
        raise SystemExit("--managed cannot be combined with --srt or --node")
    if args.managed:
        try:
            srt, node = _managed_paths()
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            srt = node = None
    else:
        srt = args.srt or shutil.which("srt")
        node = args.node or shutil.which("node")
    platform_name = _platform_name(args.platform)
    architecture = _architecture_name(args.architecture)
    if args.real:
        reports = [
            run_real_probe(
                platform_name=platform_name,
                architecture=architecture,
                srt_path=srt,
                node_path=node,
            )
            for _ in range(args.repeat)
        ]
        report = reports[0]
        fallback_counts = [item["host_fallback_count"] for item in reports]
        observed_fallbacks = [
            count for count in fallback_counts if type(count) is int
        ]
        report["host_fallback_count"] = (
            sum(observed_fallbacks)
            if len(observed_fallbacks) == len(fallback_counts)
            or any(count > 0 for count in observed_fallbacks)
            else None
        )
        report["runs"] = len(reports)
        report["passed_runs"] = sum(item["status"] == "passed" for item in reports)
        report["failed_runs"] = len(reports) - report["passed_runs"]
        if report["host_fallback_count"]:
            report["status"] = "failed"
            report["reason_code"] = "host_fallback_detected"
        elif any(item["reason_code"] == "candidate_rejected" for item in reports):
            report["status"] = "failed"
            report["reason_code"] = "candidate_rejected"
        elif any(item["status"] == "not_ready" for item in reports):
            report["status"] = "not_ready"
            report["reason_code"] = next(
                item["reason_code"] for item in reports if item["status"] == "not_ready"
            )
        elif report["failed_runs"]:
            report["status"] = "failed"
            report["reason_code"] = "repeat_not_stable"
    else:
        report = build_report(
            platform_name=platform_name,
            architecture=architecture,
            srt_path=srt,
            node_path=node,
            real=False,
        )
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print(_render_text(report), end="")
    if report["status"] == "not_ready":
        return 2
    if report["status"] == "failed":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
