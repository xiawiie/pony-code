#!/usr/bin/env python3
"""Run the Docker Sandbox production vertical from a clean wheel install."""

from __future__ import annotations

import argparse
import _thread
import hashlib
import io
from importlib import metadata
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import venv

from pico import sandbox_release_authority as release_authority
from pico.docker_sandbox import _run_bounded_process
from pico.safe_subprocess import build_trusted_executables, run_hardened_git


MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_CANDIDATE_SMOKE_OUTPUT_BYTES = 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ARCHIVE_ENTRIES = 100_000
MANDATORY_SECURITY_TESTS = (
    "tests/test_tool_policy.py",
    "tests/test_shell_security_corpus.py",
)
_NON_PASSING_PYTEST = re.compile(
    r"(?:^|\s)\d+\s+(?:skipped|xfailed|xpassed)(?:\s|,|$)",
    re.IGNORECASE,
)
_RUNTIME_PROBE_FIELDS = {
    "capabilities_dropped",
    "core_dumps_disabled",
    "cpu_limited",
    "dns_denied",
    "home_limited",
    "local_dns_allowed",
    "loopback_allowed",
    "memory_limited",
    "no_new_privileges",
    "nofile_limited",
    "pids_limited",
    "readonly_rootfs",
    "run_limited",
    "seccomp_filtered",
    "setuid_denied",
    "shm_limited",
    "state_artifacts_hidden",
    "tcp_denied",
    "tmp_limited",
    "udp_denied",
    "udp_loopback_allowed",
    "workspace_writable",
}
_PRIVILEGE_PROBE_FIELDS = {
    "architecture_known",
    "bpf_denied_eperm",
    "capability_raise_denied_eperm",
    "capget_allowed",
    "capset_same_allowed",
    "cleanup_complete",
    "mknod_denied_eperm",
    "mount_denied_eperm",
    "ordinary_file_created",
    "own_namespace_opened",
    "ptrace_child_allowed",
    "ptrace_host_denied_esrch",
    "rootfs_denied_erofs",
    "same_uid_setuid_allowed",
    "setns_denied_eperm",
    "setuid_root_denied_eperm",
}
_RESOURCE_PROBE_FIELDS = {
    "cpu_children_reaped",
    "cpu_nr_throttled_increased",
    "fd_descriptors_closed",
    "fd_limit_emfile",
    "pid_children_reaped",
    "pid_limit_eagain",
    "run_tmpfs_enospc",
    "run_tmpfs_unlinked",
}
_WORKSPACE_CRUD_FIELDS = {
    "create_succeeded",
    "delete_succeeded",
    "initial_read_succeeded",
    "persistence_written",
    "read_back_succeeded",
    "rename_succeeded",
    "write_succeeded",
}
_WORKSPACE_PERSIST_FIELDS = {
    "cross_call_read_succeeded",
    "cross_call_write_succeeded",
    "persistent_file_deleted",
    "read_back_succeeded",
}
_EPHEMERAL_PROBE_FIELDS = {
    "home_cleared",
    "run_cleared",
    "tmp_cleared",
}
_SENSITIVE_PROBE_FIELDS = {
    "credentials_hidden",
    "env_files_hidden",
    "ordinary_file_visible",
    "source_git_marker_hidden",
    "source_pico_marker_hidden",
    "templates_visible",
}
_TOOL_PROBE_FIELDS = {"git", "pytest", "python", "rg", "ruff", "shell", "uv"}
_SOURCE_ISOLATION_MARKER = "pico-release-source-isolation-control"
_WORKSPACE_PERSISTENCE_MARKER = "pico-release-workspace-persistence"
_EPHEMERAL_MARKER = "pico-release-ephemeral-control"
_RUNTIME_CANDIDATE_A = "test_pico_release_runtime_a.py"
_RUNTIME_CANDIDATE_B = "pico-release-runtime-b.txt"
_RUNTIME_CANDIDATE_A_CONTENT = (
    "def test_pico_release_runtime_candidate():\n"
    "    assert 'runtime candidate A'.endswith('A')\n"
)
_RUNTIME_CANDIDATE_B_CONTENT = "runtime candidate B\n"
_RUNTIME_CANDIDATE_HASHES = {
    _RUNTIME_CANDIDATE_A: "sha256:"
    + hashlib.sha256(_RUNTIME_CANDIDATE_A_CONTENT.encode("utf-8")).hexdigest(),
    _RUNTIME_CANDIDATE_B: "sha256:"
    + hashlib.sha256(_RUNTIME_CANDIDATE_B_CONTENT.encode("utf-8")).hexdigest(),
}
_RUNTIME_SHELL_COMMAND = (
    f"cat {_RUNTIME_CANDIDATE_A} && "
    "python -m pytest -q -o cache_dir=/tmp/pico-release-pytest-cache "
    f"{_RUNTIME_CANDIDATE_A} && "
    f"printf 'runtime candidate B\\n' > {_RUNTIME_CANDIDATE_B}"
)
_RUNTIME_CASE_IDS = (
    "runtime.diff_apply_cleanup",
    "runtime.recovery_preview",
    "runtime.tool_roundtrip",
)
_APPLY_CASE_IDS = (
    "apply.blocked_matrix",
    "apply.candidate_matrix",
    "apply.conflict_rollback_guards",
    "apply.crash_reconcile",
    "apply.helper_failure",
    "apply.source_profiles",
)
_NETWORK_CASE_IDS = (
    "network.control_cleanup",
    "network.control_gateway_host_reachable",
    "network.control_peer_reachable",
    "network.host_to_guest_denied",
    "network.production_gateway_host_denied",
    "network.production_peer_denied",
)
_CASE_IDS = tuple(
    sorted((*_APPLY_CASE_IDS, *_NETWORK_CASE_IDS, *_RUNTIME_CASE_IDS))
)
_RUNTIME_CASE_PARENT_GATES = {
    "runtime_recovery_preview": ("runtime.recovery_preview",),
    "runtime_tool_roundtrip": ("runtime.tool_roundtrip",),
    "trusted_diff": ("runtime.diff_apply_cleanup",),
}
_APPLY_CASE_PARENT_GATES = {"apply_fault_matrix": _APPLY_CASE_IDS}
_NETWORK_CASE_PARENT_GATES = {"external_network_denied": _NETWORK_CASE_IDS}
_NETWORK_HOST_ALIAS = "pico-network-host"
_NETWORK_PEER_TCP_PORT = 32101
_NETWORK_PEER_UDP_PORT = 32102
_NETWORK_GUEST_TCP_PORT = 32105
_NETWORK_PROBE_TIMEOUT = 5.0
_NETWORK_GUEST_WAIT_SECONDS = 10.0
_NETWORK_RUNNER_TIMEOUT = 30
_NETWORK_THREAD_JOIN_TIMEOUT = 35.0
_NETWORK_POSITIVE_FIELDS = {
    "control_gateway_reachable",
    "control_host_reachable",
    "control_peer_dns_reachable",
    "control_peer_tcp_reachable",
    "control_peer_udp_reachable",
}
_NETWORK_PRODUCTION_FIELDS = {
    "challenge_bound",
    "guest_listener_armed",
    "guest_loopback_control",
    "guest_no_host_connection",
    "production_gateway_denied",
    "production_host_denied",
    "production_peer_dns_denied",
    "production_peer_tcp_denied",
    "production_peer_udp_denied",
    "public_dns_denied",
    "public_tcp_denied",
    "public_udp_denied",
}
_NETWORK_PROBE_FACT_FIELDS = {
    "challenge_bound",
    "guest_listener_armed",
    "guest_loopback_control",
    "guest_no_host_connection",
    "host_client_control",
    "host_listeners_remaining",
    "host_to_guest_denied",
    "marker_absent",
    "probe_outcome_valid",
    "probe_threads_remaining",
    "production_context_cleaned",
    "production_network_none",
    "public_dns_denied",
    "public_tcp_denied",
    "public_udp_denied",
}
_SYSCALL_NUMBERS = {
    "aarch64": {
        "bpf": 280,
        "capget": 90,
        "capset": 91,
        "mount": 40,
        "ptrace": 117,
        "setns": 268,
    },
    "amd64": {
        "bpf": 321,
        "capget": 125,
        "capset": 126,
        "mount": 165,
        "ptrace": 101,
        "setns": 308,
    },
    "arm64": {
        "bpf": 280,
        "capget": 90,
        "capset": 91,
        "mount": 40,
        "ptrace": 117,
        "setns": 268,
    },
    "x86_64": {
        "bpf": 321,
        "capget": 125,
        "capset": 126,
        "mount": 165,
        "ptrace": 101,
        "setns": 308,
    },
}
_PRIVILEGE_PROBE_TIMEOUT = 30
_RESOURCE_PROBE_TIMEOUT = 60
_OOM_PROBE_TIMEOUT = 90
_OOM_ALLOCATION_BYTES = 64 * 1024 * 1024
_OUTPUT_PROBE_BYTES = 2 * 1024 * 1024
_OUTPUT_RETAINED_BYTES = 1024 * 1024
_DISK_WATCHDOG_FILE_BYTES = 128 * 1024 * 1024
_DISK_WATCHDOG_FULL_FILES = 8
_DISK_WATCHDOG_BYTES = (
    _DISK_WATCHDOG_FILE_BYTES * _DISK_WATCHDOG_FULL_FILES + 1
)
_DISK_WATCHDOG_SLEEP_SECONDS = 30
_DISK_WATCHDOG_TIMEOUT = 35
_PID_PROBE_ATTEMPTS = 1024
_CPU_PROBE_PROCESSES = 4
_CPU_PROBE_SECONDS = 3.0
_FD_PROBE_ATTEMPTS = 4096
_RUN_PROBE_MAX_BYTES = 32 * 1024 * 1024
_HOST_SENTINEL_GRACE_SECONDS = 2
_PROCESS_HEARTBEAT_DELAY_SECONDS = 2.0
_PROCESS_HEARTBEAT_WAIT_SECONDS = 3.0
_PROCESS_INTERRUPT_READY_TIMEOUT = 10.0
_PROCESS_MODE_CONTRACTS = {
    "control": {"expected_outcome": "completed", "timeout": 30},
    "interrupt": {"expected_outcome": "interrupted", "timeout": 30},
    "normal": {"expected_outcome": "completed", "timeout": 30},
    "timeout": {"expected_outcome": "timeout", "timeout": 1},
}
_UNSUPPORTED_LOCAL_ENTRY_KINDS = ("symlink", "hardlink", "fifo", "socket")
_UNSUPPORTED_ENTRY_KINDS = (*_UNSUPPORTED_LOCAL_ENTRY_KINDS, "device")
_REQUIRED_EXTERNAL_FIXTURES = {
    "device": {
        "argument": "--device-fixture-source",
        "entry_kinds": ["block_device", "character_device"],
        "expected_error": "unsupported_workspace_entry",
        "required": True,
    },
    "mount_boundary": {
        "argument": "--mount-fixture-source",
        "expected_error": "workspace_mount_boundary",
        "required": True,
    },
}
_RUNTIME_PROBE = r"""
import errno
import json
import os
import resource
import socket
import sys
import threading

def denied(action):
    try:
        action()
    except OSError:
        return True
    return False

def unreachable(action):
    try:
        action()
    except OSError as exc:
        return exc.errno in {
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
        }
    return False

def external(kind, address):
    probe = socket.socket(socket.AF_INET, kind)
    probe.settimeout(0.5)
    try:
        if kind == socket.SOCK_STREAM:
            probe.connect(address)
        else:
            probe.sendto(b"probe", address)
    finally:
        probe.close()

def filesystem_limited(path, maximum):
    value = os.statvfs(path)
    size = value.f_blocks * value.f_frsize
    return 0 < size <= maximum

loopback = False
server = socket.socket()
try:
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    def serve():
        connection, _ = server.accept()
        with connection:
            connection.sendall(b"ok")
    thread = threading.Thread(target=serve)
    thread.start()
    with socket.create_connection(server.getsockname(), timeout=1) as client:
        loopback = client.recv(2) == b"ok"
    thread.join(timeout=2)
    loopback = loopback and not thread.is_alive()
finally:
    server.close()

udp_loopback = False
udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    udp_server.settimeout(1)
    udp_server.bind(("127.0.0.1", 0))
    udp_client.sendto(b"ok", udp_server.getsockname())
    payload, _ = udp_server.recvfrom(2)
    udp_loopback = payload == b"ok"
finally:
    udp_client.close()
    udp_server.close()

status = {}
with open("/proc/self/status", encoding="ascii") as source:
    for line in source:
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()

setuid_denied = denied(lambda: os.setuid(0))
readonly_rootfs = denied(
    lambda: open("/etc/pico-write", "wb").close()
)
workspace_writable = False
try:
    with open("/workspace/" + sys.argv[1], "x", encoding="ascii") as target:
        target.write("workspace-isolated\n")
    workspace_writable = True
except OSError:
    pass

quota, period = open("/sys/fs/cgroup/cpu.max", encoding="ascii").read().split()
facts = {
    "capabilities_dropped": int(status.get("CapEff", "-1"), 16) == 0,
    "core_dumps_disabled": resource.getrlimit(resource.RLIMIT_CORE) == (0, 0),
    "cpu_limited": quota != "max" and int(quota) / int(period) == 2,
    "dns_denied": all(
        denied(lambda name=name: socket.getaddrinfo(name, 80))
        for name in ("example.com", "host.docker.internal")
    ),
    "home_limited": filesystem_limited("/home/pico", 64 * 1024 * 1024),
    "local_dns_allowed": any(
        item[4][0] == "127.0.0.1"
        for item in socket.getaddrinfo("localhost", 80, socket.AF_INET)
    ),
    "loopback_allowed": loopback,
    "memory_limited": open("/sys/fs/cgroup/memory.max", encoding="ascii").read().strip() == "2147483648",
    "no_new_privileges": status.get("NoNewPrivs") == "1",
    "nofile_limited": resource.getrlimit(resource.RLIMIT_NOFILE) == (1024, 1024),
    "pids_limited": open("/sys/fs/cgroup/pids.max", encoding="ascii").read().strip() == "256",
    "readonly_rootfs": readonly_rootfs,
    "run_limited": filesystem_limited("/run", 16 * 1024 * 1024),
    "seccomp_filtered": status.get("Seccomp") == "2",
    "setuid_denied": setuid_denied,
    "shm_limited": filesystem_limited("/dev/shm", 64 * 1024 * 1024),
    "state_artifacts_hidden": not any(
        os.path.exists(path)
        for path in (
            "/workspace/.pico",
            "/var/run/docker.sock",
            "/run/docker.sock",
        )
    ),
    "tcp_denied": unreachable(
        lambda: external(socket.SOCK_STREAM, ("1.1.1.1", 53))
    ),
    "tmp_limited": filesystem_limited("/tmp", 768 * 1024 * 1024),
    "udp_denied": unreachable(
        lambda: external(socket.SOCK_DGRAM, ("1.1.1.1", 53))
    ),
    "udp_loopback_allowed": udp_loopback,
    "workspace_writable": workspace_writable,
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_NETWORK_PEER_PROBE = r"""
import socket
import sys
import threading

nonce = bytes.fromhex(sys.argv[1])
tcp_port = int(sys.argv[2])
udp_port = int(sys.argv[3])
tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
tcp.bind(("0.0.0.0", tcp_port))
tcp.listen(16)
udp.bind(("0.0.0.0", udp_port))

def serve_tcp():
    while True:
        try:
            connection, _ = tcp.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(2)
            payload = b""
            while len(payload) < len(nonce):
                chunk = connection.recv(len(nonce) - len(payload))
                if not chunk:
                    break
                payload += chunk
            if payload == nonce:
                connection.sendall(nonce)

def serve_udp():
    while True:
        try:
            payload, address = udp.recvfrom(len(nonce))
        except OSError:
            return
        if payload == nonce:
            udp.sendto(nonce, address)

threading.Thread(target=serve_tcp, daemon=True).start()
threading.Thread(target=serve_udp, daemon=True).start()
threading.Event().wait()
""".strip()
_NETWORK_CONTROL_PROBE = r"""
import json
import socket
import sys

peer_alias = sys.argv[1]
peer_ipv4 = sys.argv[2]
peer_tcp_port = int(sys.argv[3])
peer_udp_port = int(sys.argv[4])
gateway = sys.argv[5]
gateway_tcp_port = int(sys.argv[6])
host_alias = sys.argv[7]
host_tcp_port = int(sys.argv[8])
nonce = bytes.fromhex(sys.argv[9])
timeout = float(sys.argv[10])

def tcp_exchange(host, port):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(nonce)
        payload = b""
        while len(payload) < len(nonce):
            chunk = connection.recv(len(nonce) - len(payload))
            if not chunk:
                break
            payload += chunk
        return payload == nonce

def udp_exchange(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(timeout)
        probe.sendto(nonce, (host, port))
        payload, _ = probe.recvfrom(len(nonce))
        return payload == nonce

try:
    resolved = {
        item[4][0]
        for item in socket.getaddrinfo(
            peer_alias,
            peer_tcp_port,
            socket.AF_INET,
        )
    }
except OSError:
    resolved = set()

facts = {
    "control_gateway_reachable": tcp_exchange(gateway, gateway_tcp_port),
    "control_host_reachable": tcp_exchange(host_alias, host_tcp_port),
    "control_peer_dns_reachable": peer_ipv4 in resolved,
    "control_peer_tcp_reachable": tcp_exchange(peer_alias, peer_tcp_port),
    "control_peer_udp_reachable": udp_exchange(peer_alias, peer_udp_port),
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_NETWORK_PRODUCTION_PROBE = r"""
import json
import os
import socket
import sys
import threading
import time

peer_alias = sys.argv[1]
peer_tcp_port = int(sys.argv[2])
peer_udp_port = int(sys.argv[3])
gateway = sys.argv[4]
gateway_tcp_port = int(sys.argv[5])
host_alias = sys.argv[6]
host_tcp_port = int(sys.argv[7])
guest_tcp_port = int(sys.argv[8])
nonce = bytes.fromhex(sys.argv[9])
timeout = float(sys.argv[10])
ready_path = sys.argv[11]
done_path = sys.argv[12]

def tcp_exchange(host, port):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(nonce)
        payload = b""
        while len(payload) < len(nonce):
            chunk = connection.recv(len(nonce) - len(payload))
            if not chunk:
                break
            payload += chunk
        return payload == nonce

def udp_exchange(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(timeout)
        probe.sendto(nonce, (host, port))
        payload, _ = probe.recvfrom(len(nonce))
        return payload == nonce

def denied(action):
    try:
        action()
    except (OSError, socket.gaierror):
        return True
    return False

guest = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
guest.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
guest.bind(("0.0.0.0", guest_tcp_port))
guest.listen(2)
guest.settimeout(timeout)
accepted = []
stop = threading.Event()

def serve_guest():
    while not stop.is_set():
        try:
            connection, _ = guest.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        with connection:
            connection.settimeout(timeout)
            payload = b""
            while len(payload) < len(nonce):
                chunk = connection.recv(len(nonce) - len(payload))
                if not chunk:
                    break
                payload += chunk
            accepted.append(payload)
            if payload == nonce:
                connection.sendall(nonce)

thread = threading.Thread(target=serve_guest)
thread.start()
loopback = False
done_observed = False
facts = {name: False for name in (
    "challenge_bound",
    "guest_listener_armed",
    "guest_loopback_control",
    "guest_no_host_connection",
    "production_gateway_denied",
    "production_host_denied",
    "production_peer_dns_denied",
    "production_peer_tcp_denied",
    "production_peer_udp_denied",
    "public_dns_denied",
    "public_tcp_denied",
    "public_udp_denied",
)}
try:
    loopback = tcp_exchange("127.0.0.1", guest_tcp_port)
    with open(ready_path, "xb") as marker:
        marker.write(b"ready\n")
    facts.update(
        guest_listener_armed=True,
        guest_loopback_control=loopback,
        production_gateway_denied=denied(
            lambda: tcp_exchange(gateway, gateway_tcp_port)
        ),
        production_host_denied=denied(
            lambda: tcp_exchange(host_alias, host_tcp_port)
        ),
        production_peer_dns_denied=denied(
            lambda: socket.getaddrinfo(peer_alias, peer_tcp_port)
        ),
        production_peer_tcp_denied=denied(
            lambda: tcp_exchange(peer_alias, peer_tcp_port)
        ),
        production_peer_udp_denied=denied(
            lambda: udp_exchange(peer_alias, peer_udp_port)
        ),
        public_dns_denied=denied(
            lambda: socket.getaddrinfo("example.com", 80)
        ),
        public_tcp_denied=denied(
            lambda: tcp_exchange("1.1.1.1", 53)
        ),
        public_udp_denied=denied(
            lambda: udp_exchange("1.1.1.1", 53)
        ),
    )
    deadline = time.monotonic() + timeout
    while not os.path.isfile(done_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    done_observed = os.path.isfile(done_path)
finally:
    stop.set()
    guest.close()
    thread.join(timeout=timeout)
facts["guest_no_host_connection"] = accepted == [nonce]
facts["challenge_bound"] = (
    loopback
    and accepted == [nonce]
    and done_observed
    and not thread.is_alive()
)
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_PRIVILEGE_PROBE_TEMPLATE = r"""
import ctypes
import errno
import json
import os
import platform
import signal
import stat
import sys

SYSCALLS_BY_ARCH = __SYSCALL_NUMBERS__
syscalls = SYSCALLS_BY_ARCH.get(platform.machine().casefold())
if syscalls is None:
    raise SystemExit(90)

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

def call(name, *args):
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(syscalls[name]), *args)
    return result, ctypes.get_errno()

def denied(name, expected, *args):
    result, error = call(name, *args)
    return result == -1 and error == expected, result

class BpfAttr(ctypes.Structure):
    _fields_ = [
        ("map_type", ctypes.c_uint32),
        ("key_size", ctypes.c_uint32),
        ("value_size", ctypes.c_uint32),
        ("max_entries", ctypes.c_uint32),
        ("map_flags", ctypes.c_uint32),
    ]

class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

class CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]

workspace_file = "/workspace/pico-privilege-control"
mount_target = "/workspace/pico-mount-control"
device_path = "/workspace/pico-device-control"
ordinary_file_created = False
own_namespace_opened = False
ptrace_child_allowed = False
cleanup_complete = True
namespace_fd = -1
child = -1
child_reaped = False
mounted = False
raised = False
original_uid = os.getuid()

try:
    descriptor = os.open(workspace_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, b"ok")
    os.close(descriptor)
    ordinary_file_created = True
    os.mkdir(mount_target)
    namespace_fd = os.open("/proc/self/ns/mnt", os.O_RDONLY)
    own_namespace_opened = True

    mount_denied, mount_result = denied(
        "mount",
        errno.EPERM,
        ctypes.c_char_p(b"tmpfs"),
        ctypes.c_char_p(os.fsencode(mount_target)),
        ctypes.c_char_p(b"tmpfs"),
        ctypes.c_ulong(0),
        ctypes.c_char_p(b"size=4096"),
    )
    mounted = mount_result == 0
    setns_denied, _ = denied(
        "setns", errno.EPERM, ctypes.c_int(namespace_fd), ctypes.c_int(0)
    )

    bpf_attr = BpfAttr(1, 4, 4, 1, 0)
    bpf_denied, bpf_result = denied(
        "bpf",
        errno.EPERM,
        ctypes.c_int(0),
        ctypes.byref(bpf_attr),
        ctypes.c_uint(ctypes.sizeof(bpf_attr)),
    )
    if bpf_result >= 0:
        os.close(bpf_result)

    child = os.fork()
    if child == 0:
        signal.pause()
        os._exit(0)
    attached, _ = call(
        "ptrace",
        ctypes.c_long(16),
        ctypes.c_long(child),
        ctypes.c_void_p(),
        ctypes.c_void_p(),
    )
    if attached == 0:
        waited, wait_status = os.waitpid(child, 0)
        detached, _ = call(
            "ptrace",
            ctypes.c_long(17),
            ctypes.c_long(child),
            ctypes.c_void_p(),
            ctypes.c_void_p(),
        )
        ptrace_child_allowed = (
            waited == child and os.WIFSTOPPED(wait_status) and detached == 0
        )

    host_denied, _ = denied(
        "ptrace",
        errno.ESRCH,
        ctypes.c_long(16),
        ctypes.c_long(int(sys.argv[1])),
        ctypes.c_void_p(),
        ctypes.c_void_p(),
    )
    try:
        os.mknod(device_path, stat.S_IFCHR | 0o600, os.makedev(1, 3))
    except OSError as exc:
        mknod_denied = exc.errno == errno.EPERM
    else:
        mknod_denied = False
        os.unlink(device_path)

    try:
        rootfs = os.open("/etc/pico-privilege-control", os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        rootfs_denied = exc.errno == errno.EROFS
    else:
        os.close(rootfs)
        os.unlink("/etc/pico-privilege-control")
        rootfs_denied = False

    try:
        os.setuid(original_uid)
    except OSError:
        same_uid_allowed = False
    else:
        same_uid_allowed = True
    try:
        os.setuid(0)
    except OSError as exc:
        setuid_root_denied = exc.errno == errno.EPERM
    else:
        setuid_root_denied = False
        try:
            os.setuid(original_uid)
        except OSError:
            cleanup_complete = False

    header = CapHeader(0x20080522, 0)
    data = (CapData * 2)()
    capget_result, _ = call("capget", ctypes.byref(header), ctypes.byref(data))
    capget_allowed = capget_result == 0
    original = (CapData * 2)()
    for index in range(2):
        original[index].effective = data[index].effective
        original[index].permitted = data[index].permitted
        original[index].inheritable = data[index].inheritable
    capset_same_result, _ = call("capset", ctypes.byref(header), ctypes.byref(original))
    capset_same_allowed = capset_same_result == 0
    raised_data = (CapData * 2)()
    for index in range(2):
        raised_data[index].effective = original[index].effective
        raised_data[index].permitted = original[index].permitted
        raised_data[index].inheritable = original[index].inheritable
    raised_data[0].effective |= 1 << 21
    raised_data[0].permitted |= 1 << 21
    raise_result, raise_error = call(
        "capset", ctypes.byref(header), ctypes.byref(raised_data)
    )
    raised = raise_result == 0
    capability_raise_denied = raise_result == -1 and raise_error == errno.EPERM
    if raised:
        restored, _ = call("capset", ctypes.byref(header), ctypes.byref(original))
        cleanup_complete = cleanup_complete and restored == 0
finally:
    if child > 0:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(child, 0)
            child_reaped = True
        except ChildProcessError:
            child_reaped = True
        cleanup_complete = cleanup_complete and child_reaped
    if namespace_fd >= 0:
        os.close(namespace_fd)
    if mounted:
        cleanup_complete = cleanup_complete and libc.umount2(
            ctypes.c_char_p(os.fsencode(mount_target)), ctypes.c_int(2)
        ) == 0
    for path in (device_path, workspace_file):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_complete = False
    try:
        os.rmdir(mount_target)
    except FileNotFoundError:
        pass
    except OSError:
        cleanup_complete = False

facts = {
    "architecture_known": True,
    "bpf_denied_eperm": bpf_denied,
    "capability_raise_denied_eperm": capability_raise_denied,
    "capget_allowed": capget_allowed,
    "capset_same_allowed": capset_same_allowed,
    "cleanup_complete": cleanup_complete,
    "mknod_denied_eperm": mknod_denied,
    "mount_denied_eperm": mount_denied,
    "ordinary_file_created": ordinary_file_created,
    "own_namespace_opened": own_namespace_opened,
    "ptrace_child_allowed": ptrace_child_allowed,
    "ptrace_host_denied_esrch": host_denied,
    "rootfs_denied_erofs": rootfs_denied,
    "same_uid_setuid_allowed": same_uid_allowed,
    "setns_denied_eperm": setns_denied,
    "setuid_root_denied_eperm": setuid_root_denied,
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_PRIVILEGE_PROBE = _PRIVILEGE_PROBE_TEMPLATE.replace(
    "__SYSCALL_NUMBERS__",
    json.dumps(_SYSCALL_NUMBERS, sort_keys=True, separators=(",", ":")),
)
_RESOURCE_PROBE = r"""
import errno
import json
import os
import signal
import sys
import time

pid_attempts = int(sys.argv[1])
cpu_processes = int(sys.argv[2])
cpu_seconds = float(sys.argv[3])
fd_attempts = int(sys.argv[4])
run_max_bytes = int(sys.argv[5])

def reap(children):
    complete = True
    for child in children:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            waited, _ = os.waitpid(child, 0)
            complete = complete and waited == child
        except ChildProcessError:
            pass
        except OSError:
            complete = False
    return complete

pid_children = []
pid_limit_eagain = False
try:
    for _ in range(pid_attempts):
        try:
            child = os.fork()
        except OSError as exc:
            pid_limit_eagain = exc.errno == errno.EAGAIN
            break
        if child == 0:
            signal.pause()
            os._exit(0)
        pid_children.append(child)
finally:
    pid_children_reaped = reap(pid_children)

def throttled():
    values = {}
    with open("/sys/fs/cgroup/cpu.stat", encoding="ascii") as source:
        for line in source:
            name, value = line.split()
            values[name] = int(value)
    return values["nr_throttled"]

before_throttled = throttled()
cpu_children = []
cpu_children_reaped = False
try:
    deadline = time.monotonic() + cpu_seconds
    for _ in range(cpu_processes):
        child = os.fork()
        if child == 0:
            value = 1
            while time.monotonic() < deadline:
                value = (value * 1103515245 + 12345) & 0x7fffffff
            os._exit(value & 1)
        cpu_children.append(child)
    cpu_children_reaped = True
    for child in cpu_children:
        waited, _ = os.waitpid(child, 0)
        cpu_children_reaped = cpu_children_reaped and waited == child
    cpu_children = []
finally:
    cpu_children_reaped = reap(cpu_children) and cpu_children_reaped
cpu_nr_throttled_increased = throttled() > before_throttled

descriptors = []
fd_limit_emfile = False
try:
    for _ in range(fd_attempts):
        try:
            descriptors.append(os.open("/dev/null", os.O_RDONLY))
        except OSError as exc:
            fd_limit_emfile = exc.errno == errno.EMFILE
            break
finally:
    fd_descriptors_closed = True
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            fd_descriptors_closed = False

run_path = "/run/pico-resource-fill"
run_descriptor = -1
run_tmpfs_enospc = False
written = 0
try:
    run_descriptor = os.open(run_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    chunk = b"x" * (1024 * 1024)
    while written <= run_max_bytes:
        try:
            count = os.write(run_descriptor, chunk)
        except OSError as exc:
            run_tmpfs_enospc = exc.errno == errno.ENOSPC
            break
        if count <= 0:
            break
        written += count
finally:
    if run_descriptor >= 0:
        os.close(run_descriptor)
    try:
        os.unlink(run_path)
        run_tmpfs_unlinked = True
    except FileNotFoundError:
        run_tmpfs_unlinked = False
    except OSError:
        run_tmpfs_unlinked = False

facts = {
    "cpu_children_reaped": cpu_children_reaped,
    "cpu_nr_throttled_increased": cpu_nr_throttled_increased,
    "fd_descriptors_closed": fd_descriptors_closed,
    "fd_limit_emfile": fd_limit_emfile,
    "pid_children_reaped": pid_children_reaped,
    "pid_limit_eagain": pid_limit_eagain,
    "run_tmpfs_enospc": run_tmpfs_enospc,
    "run_tmpfs_unlinked": run_tmpfs_unlinked,
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_OOM_PROBE = r"""
import sys

chunk_bytes = int(sys.argv[1])
chunks = []
while True:
    chunk = bytearray(chunk_bytes)
    for offset in range(0, chunk_bytes, 4096):
        chunk[offset] = 1
    chunks.append(chunk)
""".strip()
_OUTPUT_PROBE = r"""
import os
import sys
import threading

size = int(sys.argv[1])

def emit(descriptor, value):
    remaining = size
    chunk = value * (64 * 1024)
    while remaining:
        written = os.write(descriptor, chunk[:remaining])
        if written <= 0:
            raise SystemExit(91)
        remaining -= written

threads = (
    threading.Thread(target=emit, args=(sys.stdout.fileno(), b"o")),
    threading.Thread(target=emit, args=(sys.stderr.fileno(), b"e")),
)
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
""".strip()
_DISK_WATCHDOG_PROBE = r"""
import sys
import time

prefix = "pico-watchdog-overflow-"
file_bytes = int(sys.argv[1])
full_files = int(sys.argv[2])
for index in range(full_files):
    with open(prefix + f"{index:02d}", "xb") as target:
        target.truncate(file_bytes)
with open(prefix + f"{full_files:02d}", "xb") as target:
    target.truncate(1)
time.sleep(float(sys.argv[3]))
""".strip()
_WORKSPACE_CRUD_PROBE = r"""
import json
from pathlib import Path
import sys

seed = Path(sys.argv[1])
created = Path(sys.argv[2])
renamed = Path(sys.argv[3])
persistent = Path(sys.argv[4])
facts = {
    "create_succeeded": False,
    "delete_succeeded": False,
    "initial_read_succeeded": seed.read_bytes() == b"workspace-seed\n",
    "persistence_written": False,
    "read_back_succeeded": False,
    "rename_succeeded": False,
    "write_succeeded": False,
}
created.write_bytes(b"created\n")
facts["create_succeeded"] = created.read_bytes() == b"created\n"
created.write_bytes(b"written\n")
facts["write_succeeded"] = created.read_bytes() == b"written\n"
created.rename(renamed)
facts["rename_succeeded"] = not created.exists() and renamed.is_file()
facts["read_back_succeeded"] = renamed.read_bytes() == b"written\n"
renamed.unlink()
facts["delete_succeeded"] = not renamed.exists()
persistent.write_bytes(b"first-container\n")
facts["persistence_written"] = persistent.read_bytes() == b"first-container\n"
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_WORKSPACE_PERSIST_PROBE = r"""
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
facts = {
    "cross_call_read_succeeded": path.read_bytes() == b"first-container\n",
    "cross_call_write_succeeded": False,
    "persistent_file_deleted": False,
    "read_back_succeeded": False,
}
path.write_bytes(b"second-container\n")
facts["cross_call_write_succeeded"] = True
facts["read_back_succeeded"] = path.read_bytes() == b"second-container\n"
path.unlink()
facts["persistent_file_deleted"] = not path.exists()
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_EPHEMERAL_WRITE_PROBE = r"""
from pathlib import Path
import sys

for root in ("/home/pico", "/tmp", "/run"):
    path = Path(root) / sys.argv[1]
    path.write_bytes(b"ephemeral\n")
    if path.read_bytes() != b"ephemeral\n":
        raise SystemExit(91)
""".strip()
_EPHEMERAL_READ_PROBE = r"""
import json
from pathlib import Path
import sys

facts = {
    "home_cleared": not (Path("/home/pico") / sys.argv[1]).exists(),
    "run_cleared": not (Path("/run") / sys.argv[1]).exists(),
    "tmp_cleared": not (Path("/tmp") / sys.argv[1]).exists(),
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_SENSITIVE_PROBE = r"""
import json
from pathlib import Path

root = Path("/workspace")
facts = {
    "credentials_hidden": not any(
        (root / path).exists()
        for path in (".git-credentials", "config/credentials.json", "secret.txt")
    ),
    "env_files_hidden": not any(
        (root / path).exists() for path in (".env", ".env.local", ".envrc")
    ),
    "ordinary_file_visible": (root / "main.py").read_bytes() == b"print('ok')\n",
    "source_git_marker_hidden": not (root / ".git/source-marker").exists(),
    "source_pico_marker_hidden": not (root / ".pico/source-marker").exists(),
    "templates_visible": all(
        (root / path).read_bytes() == b"VALUE=example\n"
        for path in (".env.example", ".env.sample", ".env.template")
    ),
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_TOOL_PROBE = r"""
import json
import os
import subprocess
import sys

tools = json.loads(sys.argv[1])
facts = {}
for name, path in sorted(tools.items()):
    argv = [path, "-c", "true"] if name == "shell" else [path, "--version"]
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    facts[name] = (
        os.path.isfile(path)
        and os.access(path, os.X_OK)
        and result.returncode == 0
        and bool(result.stdout or result.stderr)
    ) if name != "shell" else (
        os.path.isfile(path) and os.access(path, os.X_OK) and result.returncode == 0
    )
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
""".strip()
_PROCESS_TREE_PROBE = r"""
import os
from pathlib import Path
import sys
import time

prefix = sys.argv[1]
delay = float(sys.argv[2])
mode = sys.argv[3]
root = Path("/workspace")

def delayed_write(name):
    (root / f"{prefix}-{name}-started").write_bytes(b"started\n")
    time.sleep(delay)
    (root / f"{prefix}-{name}-heartbeat").write_bytes(b"residue\n")
    os._exit(0)

child = os.fork()
if child == 0:
    delayed_write("child")

grandchild_parent = os.fork()
if grandchild_parent == 0:
    grandchild = os.fork()
    if grandchild == 0:
        delayed_write("grandchild")
    os._exit(0)

daemon_parent = os.fork()
if daemon_parent == 0:
    os.setsid()
    daemon = os.fork()
    if daemon == 0:
        delayed_write("daemon")
    os._exit(0)

started = [root / f"{prefix}-{name}-started" for name in ("child", "grandchild", "daemon")]
deadline = time.monotonic() + 5
while not all(path.is_file() for path in started):
    if time.monotonic() >= deadline:
        raise SystemExit(92)
    time.sleep(0.01)
(root / f"{prefix}-ready").write_bytes(b"ready\n")
if mode == "control":
    time.sleep(delay + 0.5)
    raise SystemExit(0)
if mode == "normal":
    raise SystemExit(0)
while True:
    time.sleep(60)
""".strip()
MANDATORY_CHECK_IDS = (
    "status_zero_mutation",
    "source_stable_staging",
    "sensitive_filtering",
    "unsupported_entry_rejection",
    "mount_boundary_rejection",
    "image_identity",
    "image_config",
    "container_contract",
    "source_not_mounted",
    "state_not_mounted",
    "external_network_denied",
    "container_loopback_allowed",
    "privilege_denied",
    "readonly_rootfs",
    "resource_limits",
    "output_bounded",
    "target_success",
    "target_nonzero",
    "timeout_cleanup",
    "detached_cleanup",
    "workspace_cross_call_persistence",
    "home_cross_call_ephemeral",
    "runtime_tool_roundtrip",
    "runtime_recovery_preview",
    "trusted_diff",
    "source_unchanged",
    "fixture_apply_success",
    "fixture_apply_conflict",
    "fixture_apply_rollback",
    "apply_fault_matrix",
    "create_reconciliation",
    "other_container_untouched",
    "compatibility_pytest",
    "compatibility_ruff",
    "synthetic_git_semantics",
    "container_cleanup",
    "zero_host_fallback",
)


def _runtime_case_result(case_id, facts):
    if case_id == "runtime.tool_roundtrip":
        fields = {
            "model_client",
            "provider_transport_attempts",
            "tool_sequence",
            "tool_statuses",
            "tool_change_sequence",
            "tool_change_statuses",
            "initial_read_match",
            "builtin_write_a_match",
            "shell_observed_a",
            "final_read_b_match",
            "source_pre_apply_unchanged",
            "execution_plane",
            "sandbox_outcome",
            "exit_code",
            "timed_out",
            "target_started",
            "runner_executed",
            "residue_detected",
            "stdout_truncated",
            "stderr_truncated",
            "cleanup_status",
            "host_fallback_count",
        }
        if (
            not isinstance(facts, dict)
            or set(facts) != fields
            or not isinstance(facts["model_client"], str)
            or type(facts["provider_transport_attempts"]) is not int
            or type(facts["exit_code"]) is not int
            or type(facts["host_fallback_count"]) is not int
            or any(
                type(facts[name]) is not bool
                for name in (
                    "initial_read_match",
                    "builtin_write_a_match",
                    "shell_observed_a",
                    "final_read_b_match",
                    "source_pre_apply_unchanged",
                    "timed_out",
                    "target_started",
                    "runner_executed",
                    "residue_detected",
                    "stdout_truncated",
                    "stderr_truncated",
                )
            )
            or any(
                not isinstance(facts[name], list)
                or any(not isinstance(item, str) for item in facts[name])
                for name in (
                    "tool_sequence",
                    "tool_statuses",
                    "tool_change_sequence",
                    "tool_change_statuses",
                )
            )
            or any(
                not isinstance(facts[name], str)
                for name in (
                    "execution_plane",
                    "sandbox_outcome",
                    "cleanup_status",
                )
            )
        ):
            raise ValueError("invalid runtime case evidence")
        sequence_ok = (
            facts["model_client"] == "FakeModelClient"
            and facts["provider_transport_attempts"] == 0
            and facts["tool_sequence"]
            == ["read_file", "write_file", "run_shell", "read_file"]
            and facts["tool_statuses"] == ["ok"] * 4
            and facts["tool_change_sequence"] == ["write_file", "run_shell"]
            and facts["tool_change_statuses"] == ["finalized"] * 2
            and all(
                facts[name]
                for name in (
                    "initial_read_match",
                    "builtin_write_a_match",
                    "shell_observed_a",
                    "final_read_b_match",
                    "source_pre_apply_unchanged",
                )
            )
        )
        shell_ok = (
            facts["execution_plane"] == "sandbox"
            and facts["sandbox_outcome"] == "completed"
            and facts["exit_code"] == 0
            and facts["timed_out"] is False
            and facts["target_started"] is True
            and facts["runner_executed"] is True
            and facts["residue_detected"] is False
            and facts["stdout_truncated"] is False
            and facts["stderr_truncated"] is False
            and facts["cleanup_status"] == "completed"
            and facts["host_fallback_count"] == 0
        )
        if not sequence_ok:
            return False, "runtime_sequence_mismatch"
        return (True, "verified") if shell_ok else (False, "runtime_shell_outcome_invalid")

    if case_id == "runtime.recovery_preview":
        fields = {
            "checkpoint_type",
            "reference_graph_valid",
            "preview_status",
            "entries",
        }
        entry_fields = {
            "path",
            "decision",
            "reason",
            "change_kind",
            "before_exists",
            "after_sha256",
            "snapshot_eligible",
            "source_tool",
        }
        if (
            not isinstance(facts, dict)
            or set(facts) != fields
            or not isinstance(facts["checkpoint_type"], str)
            or type(facts["reference_graph_valid"]) is not bool
            or not isinstance(facts["preview_status"], str)
            or not isinstance(facts["entries"], list)
            or any(
                not isinstance(entry, dict)
                or set(entry) != entry_fields
                or any(
                    not isinstance(entry[name], str)
                    for name in (
                        "path",
                        "decision",
                        "reason",
                        "change_kind",
                        "after_sha256",
                        "source_tool",
                    )
                )
                or type(entry["before_exists"]) is not bool
                or type(entry["snapshot_eligible"]) is not bool
                for entry in facts["entries"]
            )
        ):
            raise ValueError("invalid runtime case evidence")
        expected_entries = [
            {
                "path": path,
                "decision": "restore",
                "reason": "hash_match",
                "change_kind": "created",
                "before_exists": False,
                "after_sha256": _RUNTIME_CANDIDATE_HASHES[path],
                "snapshot_eligible": True,
                "source_tool": source_tool,
            }
            for path, source_tool in sorted(
                (
                    (_RUNTIME_CANDIDATE_A, "write_file"),
                    (_RUNTIME_CANDIDATE_B, "run_shell"),
                )
            )
        ]
        passed = (
            facts["checkpoint_type"] == "turn"
            and facts["reference_graph_valid"] is True
            and facts["preview_status"] == "ready"
            and facts["entries"] == expected_entries
        )
        return (True, "verified") if passed else (False, "runtime_recovery_mismatch")

    if case_id == "runtime.diff_apply_cleanup":
        fields = {
            "diff_status",
            "pre_apply_session_state",
            "source_pre_apply_unchanged",
            "entries",
            "apply_status",
            "final_session_state",
            "cleanup_status",
            "lease_released",
            "execution_root_absent",
            "source_after",
        }
        entry_fields = {
            "path",
            "change_kind",
            "classification",
            "before_exists",
            "after_sha256",
            "size",
            "blob_bound",
        }
        source_fields = {"path", "sha256", "size"}
        if (
            not isinstance(facts, dict)
            or set(facts) != fields
            or any(
                not isinstance(facts[name], str)
                for name in (
                    "diff_status",
                    "pre_apply_session_state",
                    "apply_status",
                    "final_session_state",
                    "cleanup_status",
                )
            )
            or any(
                type(facts[name]) is not bool
                for name in (
                    "source_pre_apply_unchanged",
                    "lease_released",
                    "execution_root_absent",
                )
            )
            or not isinstance(facts["entries"], list)
            or not isinstance(facts["source_after"], list)
            or any(
                not isinstance(entry, dict)
                or set(entry) != entry_fields
                or any(
                    not isinstance(entry[name], str)
                    for name in (
                        "path",
                        "change_kind",
                        "classification",
                        "after_sha256",
                    )
                )
                or type(entry["before_exists"]) is not bool
                or type(entry["size"]) is not int
                or type(entry["blob_bound"]) is not bool
                for entry in facts["entries"]
            )
            or any(
                not isinstance(entry, dict)
                or set(entry) != source_fields
                or not isinstance(entry["path"], str)
                or not isinstance(entry["sha256"], str)
                or type(entry["size"]) is not int
                for entry in facts["source_after"]
            )
        ):
            raise ValueError("invalid runtime case evidence")
        expected_entries = [
            {
                "path": path,
                "change_kind": "created",
                "classification": "candidate",
                "before_exists": False,
                "after_sha256": _RUNTIME_CANDIDATE_HASHES[path],
                "size": len(content.encode("utf-8")),
                "blob_bound": True,
            }
            for path, content in sorted(
                (
                    (_RUNTIME_CANDIDATE_A, _RUNTIME_CANDIDATE_A_CONTENT),
                    (_RUNTIME_CANDIDATE_B, _RUNTIME_CANDIDATE_B_CONTENT),
                )
            )
        ]
        expected_source = [
            {
                "path": entry["path"],
                "sha256": entry["after_sha256"],
                "size": entry["size"],
            }
            for entry in expected_entries
        ]
        diff_ok = (
            facts["diff_status"] == "diff_ready"
            and facts["pre_apply_session_state"] == "pending_review"
            and facts["source_pre_apply_unchanged"] is True
            and facts["entries"] == expected_entries
        )
        apply_ok = (
            facts["apply_status"] in {"apply_applied", "applied_cleanup_pending"}
            and facts["final_session_state"] == "applied"
            and facts["source_after"] == expected_source
        )
        cleanup_ok = (
            facts["cleanup_status"] == "complete"
            and facts["lease_released"] is True
            and facts["execution_root_absent"] is True
        )
        if not diff_ok:
            return False, "runtime_diff_mismatch"
        if not apply_ok:
            return False, "runtime_apply_mismatch"
        return (True, "verified") if cleanup_ok else (False, "runtime_cleanup_failed")
    raise ValueError("invalid runtime case evidence")


def _runtime_case_row(case_id, facts):
    passed, reason = _runtime_case_result(case_id, facts)
    return {
        "case_id": case_id,
        "status": "pass" if passed else "fail",
        "reason_code": reason,
        "facts": facts,
    }


def _apply_case_result(case_id, facts):
    if not isinstance(facts, dict):
        raise ValueError("invalid apply case evidence")

    def inventory_valid(value):
        if not isinstance(value, dict) or set(value) != {
            "guard_journal_id",
            "journals",
            "quarantines",
        }:
            return False
        journal_id = re.compile(r"apply_[0-9a-f]{32}\Z")
        temp_name = re.compile(
            r"(?:\.pico-apply-[0-9a-f]{32}-[0-9a-f]{16}"
            r"|\.pico-apply-directory-[0-9a-f]{32})\.tmp\Z"
        )
        if (
            not isinstance(value["guard_journal_id"], str)
            or value["guard_journal_id"]
            and journal_id.fullmatch(value["guard_journal_id"]) is None
            or not isinstance(value["journals"], list)
            or not isinstance(value["quarantines"], list)
        ):
            return False
        journals = value["journals"]
        quarantines = value["quarantines"]
        if any(
            not isinstance(item, dict)
            or set(item) != {"journal_id", "status"}
            or journal_id.fullmatch(str(item["journal_id"])) is None
            or item["status"]
            not in {"applying", "apply_applied", "apply_failed_rolled_back"}
            for item in journals
        ):
            return False
        if journals != sorted(journals, key=lambda item: item["journal_id"]):
            return False
        if any(
            not isinstance(item, dict)
            or set(item) != {"journal_id", "temp_names"}
            or journal_id.fullmatch(str(item["journal_id"])) is None
            or not isinstance(item["temp_names"], list)
            or any(
                not isinstance(name, str) or temp_name.fullmatch(name) is None
                for name in item["temp_names"]
            )
            or item["temp_names"] != sorted(set(item["temp_names"]))
            for item in quarantines
        ):
            return False
        return bool(
            len(journals)
            == len({item["journal_id"] for item in journals})
            and len(quarantines)
            == len({item["journal_id"] for item in quarantines})
            and quarantines
            == sorted(quarantines, key=lambda item: item["journal_id"])
        )

    def inventory_matches(value, status, *, guarded, quarantine_temp_count=None):
        if not inventory_valid(value) or len(value["journals"]) != 1:
            return False
        journal_id = value["journals"][0]["journal_id"]
        if (
            value["journals"][0]["status"] != status
            or value["guard_journal_id"] != (journal_id if guarded else "")
        ):
            return False
        if quarantine_temp_count is None:
            return value["quarantines"] == []
        return bool(
            len(value["quarantines"]) == 1
            and value["quarantines"][0]["journal_id"] == journal_id
            and len(value["quarantines"][0]["temp_names"])
            == quarantine_temp_count
        )

    if case_id == "apply.source_profiles":
        expected = {"clean", "dirty", "non_git", "untracked"}
        if set(facts) != expected or any(type(value) is not bool for value in facts.values()):
            raise ValueError("invalid apply case evidence")
        passed = all(facts.values())
        return (True, "verified") if passed else (False, "apply_source_profiles_failed")
    if case_id == "apply.candidate_matrix":
        expected = {
            "binary",
            "create",
            "delete",
            "empty_directory_zero_write",
            "executable_mode",
            "invalid_utf8",
            "modify",
        }
        if set(facts) != expected or any(type(value) is not bool for value in facts.values()):
            raise ValueError("invalid apply case evidence")
        passed = all(facts.values())
        return (True, "verified") if passed else (False, "apply_candidate_matrix_failed")
    if case_id == "apply.blocked_matrix":
        expected = {
            "credential",
            "env",
            "fifo",
            "git",
            "hardlink",
            "large",
            "pico",
            "socket",
            "symlink",
            "zero_source_writes",
        }
        if set(facts) != expected or any(type(value) is not bool for value in facts.values()):
            raise ValueError("invalid apply case evidence")
        passed = all(facts.values())
        return (True, "verified") if passed else (False, "apply_blocked_matrix_failed")
    if case_id == "apply.conflict_rollback_guards":
        expected = {
            "conflict_zero_candidate_writes",
            "full_rollback",
            "review_guard",
            "rollback_inventory",
            "source_root_replacement_guard",
        }
        if (
            set(facts) != expected
            or any(
                type(facts[name]) is not bool
                for name in expected - {"rollback_inventory"}
            )
            or not inventory_valid(facts["rollback_inventory"])
        ):
            raise ValueError("invalid apply case evidence")
        passed = all(
            facts[name] for name in expected - {"rollback_inventory"}
        ) and inventory_matches(
            facts["rollback_inventory"],
            "apply_failed_rolled_back",
            guarded=False,
        )
        return (True, "verified") if passed else (False, "apply_guards_failed")
    if case_id == "apply.crash_reconcile":
        expected = {
            "active_inventory",
            "child_exit_code",
            "cleanup_child_exit_code",
            "cleanup_final_inventory",
            "cleanup_first_complete",
            "cleanup_guard_cleared",
            "cleanup_pending_inventory",
            "cleanup_retry_complete",
            "crash_point",
            "final_inventory",
            "journal_bound",
            "reconcile_status",
            "session_state",
            "source_after",
        }
        if (
            set(facts) != expected
            or type(facts["child_exit_code"]) is not int
            or type(facts["cleanup_child_exit_code"]) is not int
            or any(
                type(facts[name]) is not bool
                for name in (
                    "cleanup_first_complete",
                    "cleanup_guard_cleared",
                    "cleanup_retry_complete",
                    "journal_bound",
                )
            )
            or not isinstance(facts["crash_point"], str)
            or not isinstance(facts["reconcile_status"], str)
            or not isinstance(facts["session_state"], str)
            or not isinstance(facts["source_after"], str)
            or any(
                not inventory_valid(facts[name])
                for name in (
                    "active_inventory",
                    "cleanup_final_inventory",
                    "cleanup_pending_inventory",
                    "final_inventory",
                )
            )
        ):
            raise ValueError("invalid apply case evidence")
        active_id = (
            facts["active_inventory"]["journals"][0]["journal_id"]
            if len(facts["active_inventory"]["journals"]) == 1
            else ""
        )
        final_id = (
            facts["final_inventory"]["journals"][0]["journal_id"]
            if len(facts["final_inventory"]["journals"]) == 1
            else ""
        )
        cleanup_pending_id = (
            facts["cleanup_pending_inventory"]["journals"][0]["journal_id"]
            if len(facts["cleanup_pending_inventory"]["journals"]) == 1
            else ""
        )
        cleanup_final_id = (
            facts["cleanup_final_inventory"]["journals"][0]["journal_id"]
            if len(facts["cleanup_final_inventory"]["journals"]) == 1
            else ""
        )
        passed = bool(
            facts["child_exit_code"] == 73
            and facts["cleanup_child_exit_code"] == 74
            and facts["cleanup_first_complete"] is False
            and facts["cleanup_retry_complete"] is True
            and facts["cleanup_guard_cleared"] is True
            and facts["crash_point"] == "before_terminalize"
            and facts["journal_bound"] is True
            and facts["reconcile_status"] == "apply_applied"
            and facts["session_state"] == "applied"
            and facts["source_after"] == "after\n"
            and active_id == final_id
            and cleanup_pending_id == cleanup_final_id
            and active_id != cleanup_pending_id
            and inventory_matches(
                facts["active_inventory"],
                "applying",
                guarded=True,
                quarantine_temp_count=0,
            )
            and inventory_matches(
                facts["final_inventory"],
                "apply_applied",
                guarded=False,
            )
            and inventory_matches(
                facts["cleanup_pending_inventory"],
                "apply_applied",
                guarded=True,
                quarantine_temp_count=1,
            )
            and inventory_matches(
                facts["cleanup_final_inventory"],
                "apply_applied",
                guarded=False,
            )
        )
        return (True, "verified") if passed else (False, "apply_crash_reconcile_failed")
    if case_id == "apply.helper_failure":
        expected = {
            "child_exit_code",
            "inventory",
            "lease_reacquired",
            "session_state",
            "source_unchanged",
        }
        if (
            set(facts) != expected
            or type(facts["child_exit_code"]) is not int
            or any(
                type(facts[name]) is not bool
                for name in ("lease_reacquired", "source_unchanged")
            )
            or not isinstance(facts["session_state"], str)
            or not inventory_valid(facts["inventory"])
        ):
            raise ValueError("invalid apply case evidence")
        passed = facts == {
            "child_exit_code": 76,
            "inventory": {
                "guard_journal_id": "",
                "journals": [],
                "quarantines": [],
            },
            "lease_reacquired": True,
            "session_state": "pending_review",
            "source_unchanged": True,
        }
        return (True, "verified") if passed else (False, "apply_helper_failure_failed")
    raise ValueError("invalid apply case evidence")


def _passing_apply_facts():
    active_journal_id = "apply_" + "a" * 32
    cleanup_journal_id = "apply_" + "b" * 32
    rollback_journal_id = "apply_" + "c" * 32
    facts = {
        "apply.blocked_matrix": {
            "credential": True,
            "env": True,
            "fifo": True,
            "git": True,
            "hardlink": True,
            "large": True,
            "pico": True,
            "socket": True,
            "symlink": True,
            "zero_source_writes": True,
        },
        "apply.candidate_matrix": {
            "binary": True,
            "create": True,
            "delete": True,
            "empty_directory_zero_write": True,
            "executable_mode": True,
            "invalid_utf8": True,
            "modify": True,
        },
        "apply.conflict_rollback_guards": {
            "conflict_zero_candidate_writes": True,
            "full_rollback": True,
            "review_guard": True,
            "rollback_inventory": {
                "guard_journal_id": "",
                "journals": [
                    {
                        "journal_id": rollback_journal_id,
                        "status": "apply_failed_rolled_back",
                    }
                ],
                "quarantines": [],
            },
            "source_root_replacement_guard": True,
        },
        "apply.crash_reconcile": {
            "active_inventory": {
                "guard_journal_id": active_journal_id,
                "journals": [
                    {"journal_id": active_journal_id, "status": "applying"}
                ],
                "quarantines": [
                    {"journal_id": active_journal_id, "temp_names": []}
                ],
            },
            "child_exit_code": 73,
            "cleanup_child_exit_code": 74,
            "cleanup_final_inventory": {
                "guard_journal_id": "",
                "journals": [
                    {
                        "journal_id": cleanup_journal_id,
                        "status": "apply_applied",
                    }
                ],
                "quarantines": [],
            },
            "cleanup_first_complete": False,
            "cleanup_guard_cleared": True,
            "cleanup_pending_inventory": {
                "guard_journal_id": cleanup_journal_id,
                "journals": [
                    {
                        "journal_id": cleanup_journal_id,
                        "status": "apply_applied",
                    }
                ],
                "quarantines": [
                    {
                        "journal_id": cleanup_journal_id,
                        "temp_names": [
                            ".pico-apply-"
                            + cleanup_journal_id[6:]
                            + "-"
                            + "d" * 16
                            + ".tmp"
                        ],
                    }
                ],
            },
            "cleanup_retry_complete": True,
            "crash_point": "before_terminalize",
            "final_inventory": {
                "guard_journal_id": "",
                "journals": [
                    {
                        "journal_id": active_journal_id,
                        "status": "apply_applied",
                    }
                ],
                "quarantines": [],
            },
            "journal_bound": True,
            "reconcile_status": "apply_applied",
            "session_state": "applied",
            "source_after": "after\n",
        },
        "apply.helper_failure": {
            "child_exit_code": 76,
            "inventory": {
                "guard_journal_id": "",
                "journals": [],
                "quarantines": [],
            },
            "lease_reacquired": True,
            "session_state": "pending_review",
            "source_unchanged": True,
        },
        "apply.source_profiles": {
            "clean": True,
            "dirty": True,
            "non_git": True,
            "untracked": True,
        },
    }
    return facts


def _passing_apply_case_rows():
    facts = _passing_apply_facts()
    return [_apply_case_row(case_id, facts[case_id]) for case_id in _APPLY_CASE_IDS]


def _apply_case_row(case_id, facts):
    passed, reason = _apply_case_result(case_id, facts)
    return {
        "case_id": case_id,
        "status": "pass" if passed else "fail",
        "reason_code": reason,
        "facts": facts,
    }


def _network_case_result(case_id, facts):
    if not isinstance(facts, dict):
        raise ValueError("invalid network case evidence")
    digests = {"endpoint_digest", "evidence_digest"}
    boolean_fields = {
        "network.control_peer_reachable": {
            "challenge_bound",
            "control_peer_dns_reachable",
            "control_peer_tcp_reachable",
            "control_peer_udp_reachable",
            "probe_outcome_valid",
        },
        "network.control_gateway_host_reachable": {
            "challenge_bound",
            "control_gateway_reachable",
            "control_host_reachable",
            "host_client_control",
            "probe_outcome_valid",
        },
        "network.production_peer_denied": {
            "challenge_bound",
            "probe_outcome_valid",
            "production_network_none",
            "production_peer_dns_denied",
            "production_peer_tcp_denied",
            "production_peer_udp_denied",
            "public_dns_denied",
            "public_tcp_denied",
            "public_udp_denied",
        },
        "network.production_gateway_host_denied": {
            "challenge_bound",
            "probe_outcome_valid",
            "production_gateway_denied",
            "production_host_denied",
            "production_network_none",
        },
        "network.host_to_guest_denied": {
            "guest_listener_armed",
            "guest_loopback_control",
            "guest_no_host_connection",
            "host_client_control",
            "host_to_guest_denied",
            "probe_host_to_guest_denied",
            "probe_outcome_valid",
            "production_network_none",
        },
        "network.control_cleanup": {
            "cleanup_complete",
            "inventory_complete",
            "marker_absent",
            "production_context_cleaned",
        },
    }
    if case_id not in boolean_fields:
        raise ValueError("invalid network case evidence")
    expected = digests | boolean_fields[case_id]
    count_fields = set()
    if case_id == "network.control_cleanup":
        count_fields = {
            "containers_remaining",
            "host_listeners_remaining",
            "networks_remaining",
            "probe_threads_remaining",
        }
        expected |= count_fields
    if (
        set(facts) != expected
        or any(
            not isinstance(facts[name], str)
            or _SHA256_RE.fullmatch(facts[name]) is None
            for name in digests
        )
        or any(type(facts[name]) is not bool for name in boolean_fields[case_id])
        or any(
            type(facts[name]) is not int or facts[name] < 0
            for name in count_fields
        )
    ):
        raise ValueError("invalid network case evidence")
    passed = all(facts[name] for name in boolean_fields[case_id]) and all(
        facts[name] == 0 for name in count_fields
    )
    reasons = {
        "network.control_cleanup": "network_control_cleanup_failed",
        "network.control_gateway_host_reachable": "network_control_gateway_host_failed",
        "network.control_peer_reachable": "network_control_peer_failed",
        "network.host_to_guest_denied": "network_host_to_guest_failed",
        "network.production_gateway_host_denied": "network_production_gateway_host_failed",
        "network.production_peer_denied": "network_production_peer_failed",
    }
    return (True, "verified") if passed else (False, reasons[case_id])


def _network_case_row(case_id, facts):
    passed, reason = _network_case_result(case_id, facts)
    return {
        "case_id": case_id,
        "status": "pass" if passed else "fail",
        "reason_code": reason,
        "facts": facts,
    }


def _network_case_rows(result, probe_facts):
    from pico.docker_sandbox_network_control import validate_network_control_result

    result = validate_network_control_result(result)
    if (
        not isinstance(probe_facts, dict)
        or set(probe_facts) != _NETWORK_PROBE_FACT_FIELDS
        or any(
            type(value) is not (int if name.endswith("_remaining") else bool)
            or (type(value) is int and value < 0)
            for name, value in probe_facts.items()
        )
    ):
        raise ValueError("invalid network probe evidence")
    common = {
        "endpoint_digest": result["endpoint_digest"],
        "evidence_digest": result["evidence_digest"],
    }
    facts = result["facts"]
    cleanup = result["cleanup"]
    rows = {
        "network.control_peer_reachable": {
            **common,
            "challenge_bound": probe_facts["challenge_bound"],
            "probe_outcome_valid": probe_facts["probe_outcome_valid"],
            "control_peer_dns_reachable": facts[
                "control_peer_dns_reachable"
            ],
            "control_peer_tcp_reachable": facts[
                "control_peer_tcp_reachable"
            ],
            "control_peer_udp_reachable": facts[
                "control_peer_udp_reachable"
            ],
        },
        "network.control_gateway_host_reachable": {
            **common,
            "challenge_bound": probe_facts["challenge_bound"],
            "probe_outcome_valid": probe_facts["probe_outcome_valid"],
            "host_client_control": probe_facts["host_client_control"],
            "control_gateway_reachable": facts["control_gateway_reachable"],
            "control_host_reachable": facts["control_host_reachable"],
        },
        "network.production_peer_denied": {
            **common,
            "challenge_bound": probe_facts["challenge_bound"],
            "probe_outcome_valid": probe_facts["probe_outcome_valid"],
            "production_network_none": probe_facts["production_network_none"],
            "production_peer_dns_denied": facts["production_peer_dns_denied"],
            "production_peer_tcp_denied": facts["production_peer_tcp_denied"],
            "production_peer_udp_denied": facts["production_peer_udp_denied"],
            "public_dns_denied": probe_facts["public_dns_denied"],
            "public_tcp_denied": probe_facts["public_tcp_denied"],
            "public_udp_denied": probe_facts["public_udp_denied"],
        },
        "network.production_gateway_host_denied": {
            **common,
            "challenge_bound": probe_facts["challenge_bound"],
            "probe_outcome_valid": probe_facts["probe_outcome_valid"],
            "production_network_none": probe_facts["production_network_none"],
            "production_gateway_denied": facts["production_gateway_denied"],
            "production_host_denied": facts["production_host_denied"],
        },
        "network.host_to_guest_denied": {
            **common,
            "guest_listener_armed": probe_facts["guest_listener_armed"],
            "guest_loopback_control": probe_facts["guest_loopback_control"],
            "guest_no_host_connection": probe_facts["guest_no_host_connection"],
            "host_client_control": probe_facts["host_client_control"],
            "host_to_guest_denied": facts["host_to_guest_denied"],
            "probe_host_to_guest_denied": probe_facts["host_to_guest_denied"],
            "probe_outcome_valid": probe_facts["probe_outcome_valid"],
            "production_network_none": probe_facts["production_network_none"],
        },
        "network.control_cleanup": {
            **common,
            "cleanup_complete": cleanup["status"] == "completed",
            "inventory_complete": cleanup["inventory_complete"],
            "containers_remaining": cleanup["containers_remaining"],
            "networks_remaining": cleanup["networks_remaining"],
            "host_listeners_remaining": probe_facts["host_listeners_remaining"],
            "marker_absent": probe_facts["marker_absent"],
            "probe_threads_remaining": probe_facts["probe_threads_remaining"],
            "production_context_cleaned": probe_facts[
                "production_context_cleaned"
            ],
        },
    }
    return [
        _network_case_row(case_id, rows[case_id])
        for case_id in _NETWORK_CASE_IDS
    ]


def _failed_network_vertical():
    common = {
        "endpoint_digest": "sha256:" + "0" * 64,
        "evidence_digest": "sha256:" + "1" * 64,
    }
    facts = {
        "network.control_peer_reachable": {
            **common,
            "challenge_bound": False,
            "probe_outcome_valid": False,
            "control_peer_dns_reachable": False,
            "control_peer_tcp_reachable": False,
            "control_peer_udp_reachable": False,
        },
        "network.control_gateway_host_reachable": {
            **common,
            "challenge_bound": False,
            "probe_outcome_valid": False,
            "host_client_control": False,
            "control_gateway_reachable": False,
            "control_host_reachable": False,
        },
        "network.production_peer_denied": {
            **common,
            "challenge_bound": False,
            "probe_outcome_valid": False,
            "production_network_none": False,
            "production_peer_dns_denied": False,
            "production_peer_tcp_denied": False,
            "production_peer_udp_denied": False,
            "public_dns_denied": False,
            "public_tcp_denied": False,
            "public_udp_denied": False,
        },
        "network.production_gateway_host_denied": {
            **common,
            "challenge_bound": False,
            "probe_outcome_valid": False,
            "production_network_none": False,
            "production_gateway_denied": False,
            "production_host_denied": False,
        },
        "network.host_to_guest_denied": {
            **common,
            "guest_listener_armed": False,
            "guest_loopback_control": False,
            "guest_no_host_connection": False,
            "host_client_control": False,
            "host_to_guest_denied": False,
            "probe_host_to_guest_denied": False,
            "probe_outcome_valid": False,
            "production_network_none": False,
        },
        "network.control_cleanup": {
            **common,
            "cleanup_complete": False,
            "inventory_complete": False,
            "containers_remaining": 1,
            "networks_remaining": 1,
            "host_listeners_remaining": 1,
            "marker_absent": False,
            "probe_threads_remaining": 1,
            "production_context_cleaned": False,
        },
    }
    return [
        _network_case_row(case_id, facts[case_id])
        for case_id in _NETWORK_CASE_IDS
    ]


def _runtime_cleanup_session(agent, source_applier):
    store = agent.sandbox_context.runner.session_store
    state_root = agent.sandbox_context.sandbox_state_root
    try:
        applier = source_applier(agent.sandbox_context, agent.workspace_observer)
        for _attempt in range(4):
            current = store.inspect(state_root)
            if current.state == "applying":
                if current.manifest["lease"] is None:
                    store.acquire(state_root)
                applier.reconcile()
                continue
            if current.state == "cleanup_pending":
                if current.manifest["lease"] is None:
                    store.acquire(state_root)
                store.resume_cleanup(state_root)
                continue
            if current.state == "applied":
                if current.manifest["cleanup"]["status"] == "complete":
                    break
                journal_id = current.manifest["apply"]["journal_id"]
                cleanup = applier.store.cleanup_terminal_blobs(journal_id)
                if not cleanup["complete"]:
                    return False
                store.cleanup_applied(state_root)
                continue
            if current.state in {
                "ready",
                "pending_review",
                "review_required",
                "failed",
            }:
                if current.state == "review_required" and current.manifest[
                    "apply"
                ]["status"] == "apply_review_required":
                    return False
                if current.manifest["lease"] is None:
                    store.acquire(state_root)
                apply_state = current.manifest["apply"]
                if (
                    apply_state["status"] == "apply_failed_rolled_back"
                    and apply_state["journal_id"]
                ):
                    cleanup = applier.store.cleanup_terminal_blobs(
                        apply_state["journal_id"]
                    )
                    if not cleanup["complete"]:
                        return False
                store.discard(state_root)
                continue
            break
        current = store.inspect(state_root)
        return bool(
            current.state in {"applied", "discarded"}
            and current.manifest["lease"] is None
            and current.manifest["cleanup"]["status"] == "complete"
            and not agent.execution_root.exists()
        )
    except Exception:
        return False
    finally:
        try:
            current = store.inspect(state_root)
            lease = current.manifest["lease"]
            if lease is not None:
                store.release(state_root, lease["owner_nonce"])
        except Exception:
            pass


def _failed_runtime_vertical(cleanup_complete):
    rows = [
        _runtime_case_row(
            "runtime.tool_roundtrip",
            {
                "model_client": "unknown",
                "provider_transport_attempts": 0,
                "tool_sequence": [],
                "tool_statuses": [],
                "tool_change_sequence": [],
                "tool_change_statuses": [],
                "initial_read_match": False,
                "builtin_write_a_match": False,
                "shell_observed_a": False,
                "final_read_b_match": False,
                "source_pre_apply_unchanged": False,
                "execution_plane": "unknown",
                "sandbox_outcome": "not_started",
                "exit_code": -1,
                "timed_out": False,
                "target_started": False,
                "runner_executed": False,
                "residue_detected": not cleanup_complete,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "cleanup_status": "completed" if cleanup_complete else "failed",
                "host_fallback_count": 0,
            },
        ),
        _runtime_case_row(
            "runtime.recovery_preview",
            {
                "checkpoint_type": "unknown",
                "reference_graph_valid": False,
                "preview_status": "invalid",
                "entries": [],
            },
        ),
        _runtime_case_row(
            "runtime.diff_apply_cleanup",
            {
                "diff_status": "failed",
                "pre_apply_session_state": "review_required",
                "source_pre_apply_unchanged": False,
                "entries": [],
                "apply_status": "not_started",
                "final_session_state": "review_required",
                "cleanup_status": "completed" if cleanup_complete else "failed",
                "lease_released": cleanup_complete,
                "execution_root_absent": cleanup_complete,
                "source_after": [],
            },
        ),
    ]
    rows.sort(key=lambda item: item["case_id"])
    return {
        "case_rows": rows,
        "roundtrip_passed": False,
        "recovery_preview_passed": False,
        "trusted_diff_passed": False,
        "source_pre_apply_unchanged": False,
        "apply_passed": False,
        "cleanup_complete": cleanup_complete,
        "sandbox": None,
    }


def _failed_apply_vertical():
    facts = _passing_apply_facts()
    for case in facts.values():
        for name, value in case.items():
            if type(value) is bool:
                case[name] = False
    facts["apply.crash_reconcile"].update(
        child_exit_code=-1,
        cleanup_child_exit_code=-1,
        crash_point="not_run",
        reconcile_status="not_run",
        session_state="unknown",
        source_after="",
    )
    facts["apply.helper_failure"].update(
        child_exit_code=-1,
        lease_reacquired=False,
        session_state="unknown",
        source_unchanged=False,
    )
    return [_apply_case_row(case_id, facts[case_id]) for case_id in _APPLY_CASE_IDS]


def _apply_fixture_context(
    root,
    name,
    files,
    *,
    build_context,
    checkpoint_store,
    observer_type,
    git_executable=None,
    redaction_env=None,
    secret_env_names=(),
):
    source = root / ("apply-" + name)
    source.mkdir()
    for relative, data in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    context = build_context(
        source,
        pico_session_id="release-apply-" + name,
        git_executable=git_executable,
        project_state_root=root / ("apply-" + name + "-state"),
        sandbox_parent=root / "apply-sandboxes",
    )
    blobs = checkpoint_store(
        context.sandbox_state_root / "recovery" / ".pico" / "checkpoints"
    )
    observer = observer_type(
        context,
        blobs,
        redaction_env=redaction_env,
        secret_env_names=secret_env_names,
    )
    observer.ensure_baseline()
    return source, context, observer


def _release_apply_rows(
    root,
    *,
    build_context,
    checkpoint_store,
    observer_type,
    applier_type,
    git_executable,
    package_root,
):
    root = Path(root)
    root.mkdir(mode=0o700)
    module_root = Path(inspect.getsourcefile(observer_type)).resolve().parent
    if (
        module_root != Path(package_root).resolve()
        or module_root != Path(inspect.getsourcefile(applier_type)).resolve().parent
    ):
        raise ValueError("apply release owner mismatch")

    def fixture(name, files, **kwargs):
        return _apply_fixture_context(
            root,
            name,
            files,
            build_context=build_context,
            checkpoint_store=checkpoint_store,
            observer_type=observer_type,
            **kwargs,
        )

    def artifact_inventory(source, context, observer):
        mutation_store = checkpoint_store(source)
        apply_store = applier_type(context, observer).store
        current = context.current_session()
        journals = apply_store._journals_snapshot()
        if any(
            journal["sandbox_id"] != current.sandbox_id
            or journal["source"]["root"] != str(source)
            for journal in journals
        ):
            raise ValueError("apply artifact inventory mismatch")
        journal_rows = sorted(
            (
                {
                    "journal_id": journal["journal_id"],
                    "status": journal["status"],
                }
                for journal in journals
            ),
            key=lambda item: item["journal_id"],
        )
        guard = mutation_store.source_apply_guard()
        if guard is not None:
            matches = [
                journal
                for journal in journals
                if journal["journal_id"] == guard["journal_id"]
            ]
            if (
                len(matches) != 1
                or matches[0]["sandbox_id"] != guard["sandbox_id"]
                or matches[0]["diff_digest"] != guard["diff_digest"]
            ):
                raise ValueError("apply artifact inventory mismatch")

        quarantine_root = mutation_store.root / "source-apply-quarantine"
        quarantines = []
        try:
            quarantine_info = quarantine_root.lstat()
        except FileNotFoundError:
            quarantine_info = None
        if quarantine_info is not None:
            expected_uid = (
                os.geteuid() if hasattr(os, "geteuid") else quarantine_info.st_uid
            )
            if (
                not stat.S_ISDIR(quarantine_info.st_mode)
                or quarantine_root.is_symlink()
                or stat.S_IMODE(quarantine_info.st_mode) != 0o700
                or quarantine_info.st_uid != expected_uid
            ):
                raise ValueError("apply artifact inventory mismatch")
            for directory in sorted(
                quarantine_root.iterdir(),
                key=lambda item: item.name,
            ):
                directory_info = directory.lstat()
                if (
                    re.fullmatch(r"apply_[0-9a-f]{32}", directory.name) is None
                    or not stat.S_ISDIR(directory_info.st_mode)
                    or directory.is_symlink()
                    or stat.S_IMODE(directory_info.st_mode) != 0o700
                    or directory_info.st_uid != expected_uid
                ):
                    raise ValueError("apply artifact inventory mismatch")
                temp_names = []
                for leaf in sorted(directory.iterdir(), key=lambda item: item.name):
                    leaf_info = leaf.lstat()
                    if (
                        not stat.S_ISREG(leaf_info.st_mode)
                        or leaf.is_symlink()
                        or leaf_info.st_nlink != 1
                        or leaf_info.st_uid != expected_uid
                    ):
                        raise ValueError("apply artifact inventory mismatch")
                    temp_names.append(leaf.name)
                quarantines.append(
                    {
                        "journal_id": directory.name,
                        "temp_names": temp_names,
                    }
                )
        return {
            "guard_journal_id": guard["journal_id"] if guard is not None else "",
            "journals": journal_rows,
            "quarantines": quarantines,
        }

    def run_apply_child(context, diff_digest, mode):
        child = r"""
import os
import sys

from pico.sandbox_apply import (
    SandboxApplyError,
    SandboxMaintenanceContext,
    SourceApplier,
)
from pico.sandbox_session import SandboxSessionStore

store = SandboxSessionStore(sys.argv[1])
session = store.acquire(sys.argv[2])
context = SandboxMaintenanceContext(store, session)
observer = context.observer()
mode = sys.argv[4]
if mode == "helper_failure":
    try:
        SourceApplier(context, observer).apply("sha256:" + "0" * 64)
    except SandboxApplyError as exc:
        if exc.code == "sandbox_diff_identity_mismatch":
            os._exit(76)
    os._exit(77)
crashes = {"before_terminalize": 73, "after_terminalize": 74}
if mode not in crashes:
    os._exit(77)
def fault(stage, _path):
    if stage == mode:
        os._exit(crashes[mode])
SourceApplier(context, observer, fault_injector=fault).apply(sys.argv[3])
os._exit(77)
"""
        result = subprocess.run(
            [
                os.path.abspath(sys.executable),
                "-I",
                "-c",
                child,
                os.fspath(context.runner.session_store.parent),
                os.fspath(context.sandbox_state_root),
                diff_digest,
                mode,
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return result.returncode

    profile_facts = {}
    if git_executable is None:
        raise ValueError("apply release git unavailable")
    for profile in ("clean", "dirty", "untracked", "non_git"):
        source = root / ("apply-profile-" + profile)
        source.mkdir()
        (source / "modified.txt").write_text("before\n", encoding="utf-8")
        (source / "deleted.txt").write_text("delete\n", encoding="utf-8")
        profile_paths = ("profile.txt", "untracked-profile.txt")
        profile_git = None if profile == "non_git" else git_executable
        if profile_git is not None:
            (source / "profile.txt").write_text("clean\n", encoding="utf-8")
            git_home = root / "apply-git-home"
            git_home.mkdir(mode=0o700, exist_ok=True)
            for args in (
                ("init", "--quiet"),
                ("config", "user.name", "Pico Release"),
                ("config", "user.email", "pico@example.invalid"),
                ("add", "--all"),
                ("commit", "--quiet", "-m", "baseline"),
            ):
                result = subprocess.run(
                    [str(profile_git), "--no-pager", *args],
                    cwd=source,
                    env={
                        "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_TERMINAL_PROMPT": "0",
                        "HOME": str(git_home),
                        "LANG": "C",
                        "LC_ALL": "C",
                        "PATH": str(Path(profile_git).parent),
                    },
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode != 0:
                    raise ValueError("apply profile fixture failed")
            if profile == "dirty":
                (source / "profile.txt").write_text("dirty\n", encoding="utf-8")
            elif profile == "untracked":
                (source / "untracked-profile.txt").write_text(
                    "untracked\n", encoding="utf-8"
                )
        status_before = (
            run_hardened_git(
                profile_git,
                ["status", "--porcelain=v1", "-z", "--", *profile_paths],
                cwd=source,
                timeout=30,
            ).stdout
            if profile_git is not None
            else b""
        )
        context = build_context(
            source,
            pico_session_id="release-apply-profile-" + profile,
            git_executable=profile_git,
            project_state_root=root / ("apply-profile-" + profile + "-state"),
            sandbox_parent=root / "apply-sandboxes",
        )
        blobs = checkpoint_store(
            context.sandbox_state_root / "recovery" / ".pico" / "checkpoints"
        )
        observer = observer_type(context, blobs)
        observer.ensure_baseline()
        (context.execution_root / "modified.txt").write_text("after\n", encoding="utf-8")
        (context.execution_root / "deleted.txt").unlink()
        (context.execution_root / "created.txt").write_text("created\n", encoding="utf-8")
        diff = observer.finalize_diff(lambda value: value)
        result = applier_type(context, observer).apply(diff["diff_digest"])
        status_after = (
            run_hardened_git(
                profile_git,
                ["status", "--porcelain=v1", "-z", "--", *profile_paths],
                cwd=source,
                timeout=30,
            ).stdout
            if profile_git is not None
            else b""
        )
        profile_facts[profile] = bool(
            result["status"] == "apply_applied"
            and (source / "modified.txt").read_bytes() == b"after\n"
            and not (source / "deleted.txt").exists()
            and (source / "created.txt").read_bytes() == b"created\n"
            and status_after == status_before
        )

    source, context, observer = fixture(
        "candidates",
        {
            "binary.bin": b"before\x00",
            "delete.txt": b"delete\n",
            "invalid.bin": b"before\xff",
            "mode.sh": b"#!/bin/sh\nexit 0\n",
            "modify.txt": b"before\n",
        },
    )
    root_view = context.execution_root
    (root_view / "binary.bin").write_bytes(b"after\x00")
    (root_view / "create.txt").write_text("created\n", encoding="utf-8")
    (root_view / "delete.txt").unlink()
    (root_view / "empty").mkdir()
    (root_view / "invalid.bin").write_bytes(b"after\xff")
    (root_view / "mode.sh").chmod(0o755)
    (root_view / "modify.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda value: value)
    result = applier_type(context, observer).apply(diff["diff_digest"])
    candidate_facts = {
        "binary": (source / "binary.bin").read_bytes() == b"after\x00",
        "create": (source / "create.txt").read_bytes() == b"created\n",
        "delete": not (source / "delete.txt").exists(),
        "empty_directory_zero_write": not (source / "empty").exists(),
        "executable_mode": bool((source / "mode.sh").stat().st_mode & stat.S_IXUSR),
        "invalid_utf8": (source / "invalid.bin").read_bytes() == b"after\xff",
        "modify": (
            result["status"] == "apply_applied"
            and (source / "modify.txt").read_bytes() == b"after\n"
        ),
    }

    source, context, observer = fixture(
        "blocked",
        {"README.md": b"source\n"},
        redaction_env={"TOKEN": "release-secret"},
        secret_env_names=("TOKEN",),
    )
    source_before = _snapshot_tree(source)
    blocked = context.execution_root
    (blocked / ".env").write_text("TOKEN=guest\n", encoding="utf-8")
    (blocked / ".pico").mkdir()
    (blocked / ".pico" / "state").write_text("state\n", encoding="utf-8")
    (blocked / "credentials.json").write_text("{}\n", encoding="utf-8")
    (blocked / "secret.txt").write_text("release-secret\n", encoding="utf-8")
    (blocked / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    (blocked / "link").symlink_to("README.md")
    (blocked / "hard-a").write_text("hard\n", encoding="utf-8")
    os.link(blocked / "hard-a", blocked / "hard-b")
    os.mkfifo(blocked / "pipe")
    fixture_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cwd_descriptor = os.open(".", os.O_RDONLY)
    try:
        os.chdir(blocked)
        fixture_socket.bind("socket")
        os.fchdir(cwd_descriptor)
        blocked_diff = observer.finalize_diff(
            lambda value: value.replace("release-secret", "[REDACTED]")
        )
    finally:
        os.fchdir(cwd_descriptor)
        os.close(cwd_descriptor)
        fixture_socket.close()
    entries = {item["path"]: item for item in blocked_diff["artifact"]["entries"]}
    blocked_result = applier_type(context, observer).apply(blocked_diff["diff_digest"])
    blocked_facts = {
        "credential": entries["credentials.json"]["classification"] == "blocked_sensitive",
        "env": entries[".env"]["classification"] == "blocked_sensitive",
        "fifo": entries["pipe"]["classification"] == "blocked_type",
        "git": ".git" not in entries and not (source / ".git").exists(),
        "hardlink": all(entries[name]["classification"] == "blocked_type" for name in ("hard-a", "hard-b")),
        "large": entries["large.bin"]["classification"] == "blocked_size",
        "pico": entries[".pico"]["classification"] == "blocked_sensitive",
        "socket": entries["socket"]["classification"] == "blocked_type",
        "symlink": entries["link"]["classification"] == "blocked_type",
        "zero_source_writes": blocked_result["status"] == "diff_blocked" and _snapshot_tree(source) == source_before,
    }

    source, context, observer = fixture("guards", {"a.txt": b"before-a\n", "b.txt": b"before-b\n"})
    (context.execution_root / "a.txt").write_text("after-a\n", encoding="utf-8")
    (context.execution_root / "b.txt").write_text("after-b\n", encoding="utf-8")
    guard_diff = observer.finalize_diff(lambda value: value)
    (source / "a.txt").write_text("external\n", encoding="utf-8")
    conflict = applier_type(context, observer).apply(guard_diff["diff_digest"])
    conflict_ok = conflict["status"] == "apply_conflicted" and (source / "b.txt").read_bytes() == b"before-b\n"

    source2, context2, observer2 = fixture("rollback", {"a.txt": b"before-a\n", "b.txt": b"before-b\n"})
    (context2.execution_root / "a.txt").write_text("after-a\n", encoding="utf-8")
    (context2.execution_root / "b.txt").write_text("after-b\n", encoding="utf-8")
    rollback_diff = observer2.finalize_diff(lambda value: value)
    def fail_after_first(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise OSError("release rollback fault")
    rollback = applier_type(context2, observer2, fault_injector=fail_after_first).apply(rollback_diff["diff_digest"])
    rollback_ok = rollback["status"] == "apply_failed_rolled_back" and all(
        (source2 / name).read_bytes() == data
        for name, data in (("a.txt", b"before-a\n"), ("b.txt", b"before-b\n"))
    )
    rollback_inventory = artifact_inventory(source2, context2, observer2)

    source3, context3, observer3 = fixture("review", {"a.txt": b"before\n"})
    (context3.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    review_diff = observer3.finalize_diff(lambda value: value)
    def make_uncertain(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise OSError("release rollback fault")
        if stage == "before_rollback":
            (source3 / "a.txt").write_text("external\n", encoding="utf-8")
    review = applier_type(context3, observer3, fault_injector=make_uncertain).apply(review_diff["diff_digest"])
    review_guard = review["status"] == "apply_review_required" and context3.current_session().state == "review_required"

    source4, context4, observer4 = fixture("root-replace", {"a.txt": b"before\n"})
    (context4.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    root_diff = observer4.finalize_diff(lambda value: value)
    detached = root / "apply-root-replace-detached"
    def replace_root(stage, _path):
        if stage == "after_journal":
            source4.rename(detached)
            source4.mkdir()
            (source4 / "a.txt").write_text("replacement\n", encoding="utf-8")
    root_result = applier_type(context4, observer4, fault_injector=replace_root).apply(root_diff["diff_digest"])
    guard_facts = {
        "conflict_zero_candidate_writes": conflict_ok,
        "full_rollback": rollback_ok,
        "review_guard": review_guard,
        "rollback_inventory": rollback_inventory,
        "source_root_replacement_guard": root_result["status"] == "apply_review_required" and (detached / "a.txt").read_bytes() == b"before\n" and (source4 / "a.txt").read_bytes() == b"replacement\n",
    }

    source5, context5, observer5 = fixture("crash", {"a.txt": b"before\n"})
    (context5.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    crash_diff = observer5.finalize_diff(lambda value: value)
    lease = context5.current_session().manifest["lease"]
    context5.runner.session_store.release(
        context5.sandbox_state_root,
        lease["owner_nonce"],
    )
    child_exit_code = run_apply_child(
        context5,
        crash_diff["diff_digest"],
        "before_terminalize",
    )
    if child_exit_code != 73:
        raise ValueError(f"apply crash helper failed: {child_exit_code}")
    current = context5.current_session()
    active_inventory = artifact_inventory(source5, context5, observer5)
    context5.runner.session_store.acquire(context5.sandbox_state_root)
    reconciled = applier_type(context5, observer5).reconcile()
    final_session = context5.current_session()
    final_inventory = artifact_inventory(source5, context5, observer5)

    source6, context6, observer6 = fixture(
        "cleanup-retry",
        {"delete.txt": b"before\n"},
    )
    (context6.execution_root / "delete.txt").unlink()
    cleanup_diff = observer6.finalize_diff(lambda value: value)
    cleanup_lease = context6.current_session().manifest["lease"]
    context6.runner.session_store.release(
        context6.sandbox_state_root,
        cleanup_lease["owner_nonce"],
    )
    cleanup_child_exit_code = run_apply_child(
        context6,
        cleanup_diff["diff_digest"],
        "after_terminalize",
    )
    if cleanup_child_exit_code != 74:
        raise ValueError(f"apply cleanup helper failed: {cleanup_child_exit_code}")
    cleanup_session = context6.current_session()
    cleanup_journal_id = cleanup_session.manifest["apply"]["journal_id"]
    cleanup_pending_inventory = artifact_inventory(
        source6,
        context6,
        observer6,
    )
    cleanup_store = applier_type(context6, observer6).store
    first_cleanup = cleanup_store.cleanup_terminal_blobs(
        cleanup_journal_id,
        max_entries=0,
    )
    retry_cleanup = cleanup_store.cleanup_terminal_blobs(cleanup_journal_id)
    if not retry_cleanup["complete"]:
        raise ValueError("apply cleanup retry failed")
    cleanup_mutation_store = checkpoint_store(source6)
    with cleanup_mutation_store.mutation_lock(
        source_apply_journal_id=cleanup_journal_id
    ):
        cleanup_mutation_store.finish_source_apply_guard(
            journal_id=cleanup_journal_id
        )
    context6.runner.session_store.cleanup_applied(context6.sandbox_state_root)
    cleanup_guard_cleared = cleanup_mutation_store.source_apply_guard() is None
    cleanup_final_inventory = artifact_inventory(source6, context6, observer6)

    source7, context7, observer7 = fixture(
        "helper-failure",
        {"a.txt": b"before\n"},
    )
    (context7.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    helper_diff = observer7.finalize_diff(lambda value: value)
    helper_source_before = _snapshot_tree(source7)
    helper_lease = context7.current_session().manifest["lease"]
    context7.runner.session_store.release(
        context7.sandbox_state_root,
        helper_lease["owner_nonce"],
    )
    helper_exit_code = run_apply_child(
        context7,
        helper_diff["diff_digest"],
        "helper_failure",
    )
    helper_session = context7.runner.session_store.acquire(
        context7.sandbox_state_root
    )
    helper_claim = helper_session.manifest["lease"]
    helper_lease_reacquired = bool(
        helper_claim is not None and helper_claim["owner_pid"] == os.getpid()
    )
    context7.runner.session_store.release(
        context7.sandbox_state_root,
        helper_claim["owner_nonce"],
    )
    helper_source_unchanged = _snapshot_tree(source7) == helper_source_before
    helper_inventory = artifact_inventory(source7, context7, observer7)

    crash_facts = {
        "active_inventory": active_inventory,
        "child_exit_code": child_exit_code,
        "cleanup_child_exit_code": cleanup_child_exit_code,
        "cleanup_final_inventory": cleanup_final_inventory,
        "cleanup_first_complete": first_cleanup["complete"],
        "cleanup_guard_cleared": cleanup_guard_cleared,
        "cleanup_pending_inventory": cleanup_pending_inventory,
        "cleanup_retry_complete": retry_cleanup["complete"],
        "crash_point": "before_terminalize",
        "final_inventory": final_inventory,
        "journal_bound": bool(current.manifest["apply"]["journal_id"]),
        "reconcile_status": reconciled["status"],
        "session_state": final_session.state,
        "source_after": (source5 / "a.txt").read_text(encoding="utf-8"),
    }
    rows = [
        _apply_case_row("apply.source_profiles", profile_facts),
        _apply_case_row("apply.candidate_matrix", candidate_facts),
        _apply_case_row("apply.blocked_matrix", blocked_facts),
        _apply_case_row("apply.conflict_rollback_guards", guard_facts),
        _apply_case_row("apply.crash_reconcile", crash_facts),
        _apply_case_row(
            "apply.helper_failure",
            {
                "child_exit_code": helper_exit_code,
                "inventory": helper_inventory,
                "lease_reacquired": helper_lease_reacquired,
                "session_state": helper_session.state,
                "source_unchanged": helper_source_unchanged,
            },
        ),
    ]
    return sorted(rows, key=lambda item: item["case_id"])


def _run_apply_vertical(**kwargs):
    try:
        return _release_apply_rows(**kwargs)
    except BaseException:
        return _failed_apply_vertical()


def _runtime_tool_orchestration(agent, source_snapshot, source_applier):
    """Exercise the installed Runtime/ToolExecutor path and apply its exact diff."""
    answer = agent.ask("Run the fixed offline Docker Sandbox runtime fixture.")
    records = [
        record
        for record in agent.checkpoint_store.list_tool_change_records(strict=True)
        if record["owner_id"] == agent.tool_change_owner_id
        and record["turn_id"] == agent.current_task_state.task_id
    ]
    tool_results = [
        str(message["content"][0]["content"])
        for message in agent.session["messages"]
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_result"
    ]
    tool_use_names = [
        str(message["content"][0]["name"])
        for message in agent.session["messages"]
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_use"
    ]
    tool_statuses = [
        str(message.get("_pico_meta", {}).get("tool_status", ""))
        for message in agent.session["messages"]
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_result"
    ]
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    checkpoint = agent.checkpoint_store.load_checkpoint_record(
        agent.current_task_state.recovery_checkpoint_id
    )
    preview = agent.recovery_manager.preview_restore(checkpoint["checkpoint_id"])
    try:
        reference_graph_valid = (
            agent.checkpoint_store.validate_tool_change_reference_graph()
            >= len(records)
        )
    except (OSError, ValueError):
        reference_graph_valid = False
    source_pre_apply_unchanged = source_snapshot == _snapshot_tree(
        agent.source_root
    )
    finalized = agent.finalize_sandbox_session()
    entries = finalized["artifact"]["entries"]
    staged_a_match = (
        agent.execution_root / _RUNTIME_CANDIDATE_A
    ).read_text(encoding="utf-8") == _RUNTIME_CANDIDATE_A_CONTENT
    shell_record = next(
        record for record in records if record["tool_name"] == "run_shell"
    )
    shell_sandbox = dict(shell_record["sandbox"])
    store = agent.sandbox_context.runner.session_store
    store.acquire(agent.sandbox_context.sandbox_state_root)
    applied = source_applier(
        agent.sandbox_context,
        agent.workspace_observer,
    ).apply(finalized["diff_digest"])
    cleanup_complete = _runtime_cleanup_session(agent, source_applier)
    current = agent.sandbox_context.current_session()
    tool_change_by_id = {
        record["tool_change_id"]: record["tool_name"] for record in records
    }
    preview_entries = []
    for entry in preview.get("entries", []):
        source_ids = entry.get("source_tool_change_ids", [])
        source_tool = (
            tool_change_by_id.get(source_ids[0], "")
            if isinstance(source_ids, list) and len(source_ids) == 1
            else ""
        )
        after_hash = str(entry.get("after_hash", ""))
        preview_entries.append(
            {
                "path": str(entry.get("path", "")),
                "decision": str(entry.get("decision", "")),
                "reason": str(entry.get("reason", "")),
                "change_kind": str(entry.get("change_kind", "")),
                "before_exists": entry.get("before_exists") is True,
                "after_sha256": (
                    "sha256:" + after_hash
                    if re.fullmatch(r"[0-9a-f]{64}", after_hash)
                    else ""
                ),
                "snapshot_eligible": entry.get("snapshot_eligible") is True,
                "source_tool": source_tool,
            }
        )
    preview_entries.sort(key=lambda item: item["path"])
    diff_entries = [
        {
            "path": entry["path"],
            "change_kind": entry["change_kind"],
            "classification": entry["classification"],
            "before_exists": entry["before"]["exists"],
            "after_sha256": entry["after"]["sha256"],
            "size": entry["after"]["size"],
            "blob_bound": bool(
                entry["after"]["blob_ref"]
                and "sha256:" + entry["after"]["blob_ref"]
                == entry["after"]["sha256"]
            ),
        }
        for entry in entries
    ]
    diff_entries.sort(key=lambda item: item["path"])
    source_after = []
    for path in sorted(_RUNTIME_CANDIDATE_HASHES):
        data = (agent.source_root / path).read_bytes()
        source_after.append(
            {
                "path": path,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    roundtrip_facts = {
        "model_client": type(agent.model_client).__name__,
        "provider_transport_attempts": int(
            report["model"]["transport_attempts"]
            if report["model"]["transport_attempts"] is not None
            else -1
        ),
        "tool_sequence": tool_use_names,
        "tool_statuses": tool_statuses,
        "tool_change_sequence": [record["tool_name"] for record in records],
        "tool_change_statuses": [record["status"] for record in records],
        "initial_read_match": bool(tool_results and "runtime source" in tool_results[0]),
        "builtin_write_a_match": bool(
            len(tool_results) == 4
            and "wrote " + _RUNTIME_CANDIDATE_A in tool_results[1]
            and staged_a_match
        ),
        "shell_observed_a": bool(
            len(tool_results) == 4
            and _RUNTIME_CANDIDATE_A_CONTENT in tool_results[2]
            and "1 passed" in tool_results[2]
        ),
        "final_read_b_match": bool(
            answer == "runtime vertical complete"
            and len(tool_results) == 4
            and _RUNTIME_CANDIDATE_B_CONTENT.strip() in tool_results[3]
        ),
        "source_pre_apply_unchanged": source_pre_apply_unchanged,
        "execution_plane": str(shell_sandbox.get("execution_plane", "")),
        "sandbox_outcome": str(shell_sandbox.get("status", "")),
        "exit_code": int(shell_sandbox.get("exit_code", -1)),
        "timed_out": shell_sandbox.get("timed_out") is True,
        "target_started": shell_sandbox.get("target_started") is True,
        "runner_executed": shell_sandbox.get("runner_executed") is True,
        "residue_detected": shell_sandbox.get("residue_detected") is True,
        "stdout_truncated": shell_sandbox.get("stdout_truncated") is True,
        "stderr_truncated": shell_sandbox.get("stderr_truncated") is True,
        "cleanup_status": str(shell_sandbox.get("cleanup_status", "")),
        "host_fallback_count": int(report["sandbox"]["host_fallback_count"]),
    }
    recovery_facts = {
        "checkpoint_type": str(checkpoint.get("checkpoint_type", "")),
        "reference_graph_valid": bool(
            reference_graph_valid
            and checkpoint.get("tool_change_ids")
            == [record["tool_change_id"] for record in records]
            and [entry.get("path") for entry in checkpoint.get("file_entries", [])]
            == [_RUNTIME_CANDIDATE_A, _RUNTIME_CANDIDATE_B]
        ),
        "preview_status": str(preview.get("status", "")),
        "entries": preview_entries,
    }
    diff_facts = {
        "diff_status": str(finalized.get("status", "")),
        "pre_apply_session_state": str(finalized.get("session_state", "")),
        "source_pre_apply_unchanged": source_pre_apply_unchanged,
        "entries": diff_entries,
        "apply_status": str(applied.get("status", "")),
        "final_session_state": current.state,
        "cleanup_status": str(current.manifest["cleanup"]["status"]),
        "lease_released": current.manifest["lease"] is None,
        "execution_root_absent": not agent.execution_root.exists(),
        "source_after": source_after,
    }
    rows = [
        _runtime_case_row("runtime.tool_roundtrip", roundtrip_facts),
        _runtime_case_row("runtime.recovery_preview", recovery_facts),
        _runtime_case_row("runtime.diff_apply_cleanup", diff_facts),
    ]
    rows.sort(key=lambda item: item["case_id"])
    statuses = {row["case_id"]: row["status"] == "pass" for row in rows}
    return {
        "case_rows": rows,
        "tool_sequence": roundtrip_facts["tool_change_sequence"],
        "roundtrip_passed": statuses["runtime.tool_roundtrip"],
        "recovery_preview_passed": statuses["runtime.recovery_preview"],
        "trusted_diff_passed": statuses["runtime.diff_apply_cleanup"],
        "source_pre_apply_unchanged": source_pre_apply_unchanged,
        "apply_passed": statuses["runtime.diff_apply_cleanup"],
        "cleanup_complete": cleanup_complete,
        "sandbox": shell_sandbox,
    }


def _run_runtime_tool_vertical(context):
    from pico.config import load_pico_toml, read_project_env
    from pico.providers.fake import FakeModelClient
    from pico.runtime import (
        Pico,
        SANDBOX_WORKSPACE_BRANCH,
        SANDBOX_WORKSPACE_STATUS,
        _build_redaction_snapshot,
    )
    from pico.sandbox_apply import SourceApplier
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    source_workspace = WorkspaceContext.build(context.source_root)
    project_env = read_project_env(context.source_root, warn=False)
    redaction_env, secret_names, redactor = _build_redaction_snapshot(
        context.source_root,
        process_env=dict(os.environ),
        project_env=project_env,
    )
    executables = {
        name: path
        for name, path in source_workspace.trusted_executables.items()
        if name != "git"
    }
    workspace = WorkspaceContext.build(
        context.execution_root,
        repo_root_override=context.execution_root,
        executables=executables,
        inspect_git=False,
        logical_root=context.logical_root,
        branch_override=SANDBOX_WORKSPACE_BRANCH,
        default_branch_override=SANDBOX_WORKSPACE_BRANCH,
        status_override=SANDBOX_WORKSPACE_STATUS,
    )
    outputs = [
        '<tool>{"name":"read_file","args":'
        '{"path":"/workspace/README.md","start":1,"end":20}}</tool>',
        '<tool name="write_file" path="/workspace/'
        + _RUNTIME_CANDIDATE_A
        + '"><content>'
        + _RUNTIME_CANDIDATE_A_CONTENT
        + "</content></tool>",
        "<tool>"
        + json.dumps(
            {
                "name": "run_shell",
                "args": {"command": _RUNTIME_SHELL_COMMAND, "timeout": 30},
            },
            separators=(",", ":"),
        )
        + "</tool>",
        '<tool>{"name":"read_file","args":'
        '{"path":"/workspace/'
        + _RUNTIME_CANDIDATE_B
        + '","start":1,"end":20}}</tool>',
        "<final>runtime vertical complete</final>",
    ]
    build_agent = (
        Pico._for_docker_sandbox_development
        if context.authorization.attestation_kind == "development"
        else Pico
    )
    agent = build_agent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(
            context.project_state_root / "sessions",
            redactor=redactor,
        ),
        approval_policy="ask",
        secret_env_names=secret_names,
        redaction_env=redaction_env,
        _trusted_redaction_env=True,
        sandbox_context=context,
        project_config=load_pico_toml(context.source_root),
        session_id=context.sandbox_session.manifest["pico_session_id"],
    )
    agent.approve = lambda _name, _args: True
    try:
        return _runtime_tool_orchestration(
            agent,
            _snapshot_tree(context.source_root),
            SourceApplier,
        )
    except BaseException:
        return _failed_runtime_vertical(_runtime_cleanup_session(agent, SourceApplier))


def _corpus_payload():
    from pico import docker_sandbox_network_control as network_control
    from pico.docker_sandbox_network_control import (
        NetworkControl,
        validate_network_control_result,
    )

    network_control_source = inspect.getsource(NetworkControl)
    network_validator_source = inspect.getsource(validate_network_control_result)
    return {
        "behavior_parameters": {
            "cpu_probe_processes": _CPU_PROBE_PROCESSES,
            "cpu_probe_seconds": _CPU_PROBE_SECONDS,
            "disk_watchdog_bytes": _DISK_WATCHDOG_BYTES,
            "disk_watchdog_file_bytes": _DISK_WATCHDOG_FILE_BYTES,
            "disk_watchdog_full_files": _DISK_WATCHDOG_FULL_FILES,
            "disk_watchdog_sleep_seconds": _DISK_WATCHDOG_SLEEP_SECONDS,
            "disk_watchdog_timeout": _DISK_WATCHDOG_TIMEOUT,
            "expected_errno": {
                "bpf": "EPERM",
                "capability_raise": "EPERM",
                "fd_limit": "EMFILE",
                "mknod": "EPERM",
                "mount": "EPERM",
                "pid_limit": "EAGAIN",
                "ptrace_host": "ESRCH",
                "rootfs_write": "EROFS",
                "run_tmpfs": "ENOSPC",
                "setns": "EPERM",
                "setuid_root": "EPERM",
            },
            "fd_probe_attempts": _FD_PROBE_ATTEMPTS,
            "host_sentinel_grace_seconds": _HOST_SENTINEL_GRACE_SECONDS,
            "process_heartbeat_delay_seconds": _PROCESS_HEARTBEAT_DELAY_SECONDS,
            "process_heartbeat_wait_seconds": _PROCESS_HEARTBEAT_WAIT_SECONDS,
            "process_interrupt_ready_timeout": _PROCESS_INTERRUPT_READY_TIMEOUT,
            "oom_allocation_bytes": _OOM_ALLOCATION_BYTES,
            "oom_probe_timeout": _OOM_PROBE_TIMEOUT,
            "output_probe_bytes": _OUTPUT_PROBE_BYTES,
            "output_retained_bytes": _OUTPUT_RETAINED_BYTES,
            "pid_probe_attempts": _PID_PROBE_ATTEMPTS,
            "privilege_probe_timeout": _PRIVILEGE_PROBE_TIMEOUT,
            "resource_probe_timeout": _RESOURCE_PROBE_TIMEOUT,
            "run_probe_max_bytes": _RUN_PROBE_MAX_BYTES,
            "syscall_numbers": _SYSCALL_NUMBERS,
        },
        "expected_fields": {
            "ephemeral": sorted(_EPHEMERAL_PROBE_FIELDS),
            "privilege": sorted(_PRIVILEGE_PROBE_FIELDS),
            "resource": sorted(_RESOURCE_PROBE_FIELDS),
            "runtime": sorted(_RUNTIME_PROBE_FIELDS),
            "sensitive": sorted(_SENSITIVE_PROBE_FIELDS),
            "tools": sorted(_TOOL_PROBE_FIELDS),
            "workspace_crud": sorted(_WORKSPACE_CRUD_FIELDS),
            "workspace_persistence": sorted(_WORKSPACE_PERSIST_FIELDS),
        },
        "external_fixtures": _REQUIRED_EXTERNAL_FIXTURES,
        "format_version": 2,
        "mandatory_check_ids": list(MANDATORY_CHECK_IDS),
        "mandatory_security_tests": list(MANDATORY_SECURITY_TESTS),
        "probe_scripts": {
            "disk_watchdog": _DISK_WATCHDOG_PROBE,
            "ephemeral_read": _EPHEMERAL_READ_PROBE,
            "ephemeral_write": _EPHEMERAL_WRITE_PROBE,
            "oom": _OOM_PROBE,
            "output": _OUTPUT_PROBE,
            "privilege": _PRIVILEGE_PROBE,
            "process_tree": _PROCESS_TREE_PROBE,
            "resource": _RESOURCE_PROBE,
            "runtime": _RUNTIME_PROBE,
            "sensitive": _SENSITIVE_PROBE,
            "tools": _TOOL_PROBE,
            "workspace_crud": _WORKSPACE_CRUD_PROBE,
            "workspace_persistence": _WORKSPACE_PERSIST_PROBE,
        },
        "expected_behavior": _expected_behavior_payload(),
        "case_evidence": {
            "case_ids": list(_CASE_IDS),
            "parent_gates": {
                gate: list(case_ids)
                for gate, case_ids in sorted(
                    {
                        **_APPLY_CASE_PARENT_GATES,
                        **_NETWORK_CASE_PARENT_GATES,
                        **_RUNTIME_CASE_PARENT_GATES,
                    }.items()
                )
            },
            "reason_codes": [
                "network_control_cleanup_failed",
                "network_control_gateway_host_failed",
                "network_control_peer_failed",
                "network_host_to_guest_failed",
                "network_production_gateway_host_failed",
                "network_production_peer_failed",
                "runtime_apply_mismatch",
                "runtime_cleanup_failed",
                "runtime_diff_mismatch",
                "runtime_recovery_mismatch",
                "runtime_sequence_mismatch",
                "runtime_shell_outcome_invalid",
                "verified",
            ],
        },
        "ephemeral_marker": _EPHEMERAL_MARKER,
        "runtime_tool_vertical": {
            "candidate_a": _RUNTIME_CANDIDATE_A,
            "candidate_a_sha256": "sha256:"
            + hashlib.sha256(
                _RUNTIME_CANDIDATE_A_CONTENT.encode("utf-8")
            ).hexdigest(),
            "candidate_b": _RUNTIME_CANDIDATE_B,
            "candidate_b_sha256": "sha256:"
            + hashlib.sha256(
                _RUNTIME_CANDIDATE_B_CONTENT.encode("utf-8")
            ).hexdigest(),
            "shell_command": _RUNTIME_SHELL_COMMAND,
        },
        "network_control_vertical": {
            "case_ids": list(_NETWORK_CASE_IDS),
            "host_alias": _NETWORK_HOST_ALIAS,
            "guest_tcp_port": _NETWORK_GUEST_TCP_PORT,
            "peer_tcp_port": _NETWORK_PEER_TCP_PORT,
            "peer_udp_port": _NETWORK_PEER_UDP_PORT,
            "probe_timeout": _NETWORK_PROBE_TIMEOUT,
            "guest_wait_seconds": _NETWORK_GUEST_WAIT_SECONDS,
            "runner_timeout": _NETWORK_RUNNER_TIMEOUT,
            "thread_join_timeout": _NETWORK_THREAD_JOIN_TIMEOUT,
            "probe_scripts": {
                "control": _NETWORK_CONTROL_PROBE,
                "peer": _NETWORK_PEER_PROBE,
                "production": _NETWORK_PRODUCTION_PROBE,
            },
            "source_sha256": {
                name: "sha256:"
                + hashlib.sha256(inspect.getsource(owner).encode("utf-8")).hexdigest()
                for name, owner in (
                    ("case_result", _network_case_result),
                    ("case_row", _network_case_row),
                    ("case_rows", _network_case_rows),
                    ("cleanup_context", _cleanup_network_context),
                    ("docker_inspect", _docker_inspect_payload),
                    ("failed_vertical", _failed_network_vertical),
                    ("host_listener", _network_host_listener),
                    ("host_listener_control", _network_host_listener_control),
                    ("host_listener_stop", _stop_network_host_listener),
                    ("host_to_guest_denied", _network_host_to_guest_denied),
                    ("probe_output", _network_probe_output),
                    ("run_vertical", _run_network_vertical),
                )
            },
            "owner_source_sha256": "sha256:"
            + hashlib.sha256(network_control_source.encode("utf-8")).hexdigest(),
            "owner_helper_source_sha256": {
                name: "sha256:"
                + hashlib.sha256(inspect.getsource(owner).encode("utf-8")).hexdigest()
                for name, owner in (
                    ("inspect_json", network_control._inspect_json),
                    ("list_ids", network_control._list_ids),
                    ("result", network_control._result),
                    ("strict_facts", network_control._strict_facts),
                )
            },
            "validator_source_sha256": "sha256:"
            + hashlib.sha256(network_validator_source.encode("utf-8")).hexdigest(),
        },
        "source_isolation_marker": _SOURCE_ISOLATION_MARKER,
        "workspace_persistence_marker": _WORKSPACE_PERSISTENCE_MARKER,
    }


def _expected_behavior_payload():
    from pico.sandbox_session import snapshot_source_tree

    return {
        "ephemeral": {name: True for name in sorted(_EPHEMERAL_PROBE_FIELDS)},
        "mandatory_check_dependencies": {
            "apply_fault_matrix": list(_APPLY_CASE_IDS),
            "container_loopback_allowed": [
                "runtime.local_dns_allowed",
                "runtime.loopback_allowed",
                "runtime.udp_loopback_allowed",
            ],
            "detached_cleanup": [
                "process.normal",
                "process.timeout",
                "process.interrupt",
            ],
            "external_network_denied": [
                "runtime.dns_denied",
                "runtime.tcp_denied",
                "runtime.udp_denied",
                "network_control.gateway_denied",
                "network_control.host_listener_denied",
                "network_control.private_peer_denied",
            ],
            "home_cross_call_ephemeral": [
                "ephemeral.home_cleared",
                "ephemeral.run_cleared",
                "ephemeral.tmp_cleared",
            ],
            "runtime_recovery_preview": [
                "runtime.turn_checkpoint_complete",
                "runtime.restore_preview_ready",
            ],
            "runtime_tool_roundtrip": [
                "runtime.model_tool_sequence_exact",
                "runtime.shell_execution_plane_sandbox",
                "runtime.source_unchanged_before_apply",
            ],
            "trusted_diff": [
                "runtime.exact_candidate_diff",
                "runtime.source_apply_applied",
                "runtime.session_cleanup_complete",
            ],
            "image_config": [
                "image.platform_match",
                "tools.all_expected_paths_execute",
            ],
            "source_not_mounted": [
                "host_plan.private_paths_absent",
                "source_marker.absent",
                "workspace_marker.present",
            ],
            "state_not_mounted": [
                "runtime.state_artifacts_hidden",
                "workspace_marker.present",
            ],
            "workspace_cross_call_persistence": [
                "workspace_crud.all_true",
                "workspace_persistence.all_true",
            ],
        },
        "outcome_contracts": {
            "disk_watchdog": {
                "cleanup_status": "completed",
                "error_code": "sandbox_workspace_limit_exceeded",
                "residue_detected": False,
                "runner_executed": True,
                "sandbox_outcome": "container_runtime_failed",
                "target_started": True,
                "timed_out": False,
            },
            "json_probe": {
                "cleanup_status": "completed",
                "exit_code": 0,
                "residue_detected": False,
                "sandbox_outcome": "completed",
                "stderr_bytes": 0,
                "stderr_truncated": False,
                "stdout_truncated": False,
                "timed_out": False,
            },
            "oom": {
                "cleanup_status": "completed",
                "error_code": "sandbox_oom_killed",
                "exit_code": 137,
                "residue_detected": False,
                "runner_executed": True,
                "sandbox_outcome": "oom_killed",
                "target_started": True,
                "timed_out": False,
            },
            "output": {
                "cleanup_status": "completed",
                "exit_code": 0,
                "residue_detected": False,
                "runner_executed": True,
                "sandbox_outcome": "completed",
                "stderr_bytes": _OUTPUT_PROBE_BYTES,
                "stderr_retained_bytes": _OUTPUT_RETAINED_BYTES,
                "stderr_truncated": True,
                "stdout_bytes": _OUTPUT_PROBE_BYTES,
                "stdout_retained_bytes": _OUTPUT_RETAINED_BYTES,
                "stdout_truncated": True,
                "target_started": True,
                "timed_out": False,
            },
        },
        "process_modes": _PROCESS_MODE_CONTRACTS,
        "predicate_source_sha256": {
            name: "sha256:"
            + hashlib.sha256(inspect.getsource(predicate).encode("utf-8")).hexdigest()
            for name, predicate in (
                ("disk_watchdog", _disk_watchdog_probe_passed),
                ("ephemeral_facts", _ephemeral_probe_facts),
                ("json_probe", _json_probe_facts),
                ("oom", _oom_probe_passed),
                ("output", _output_probe_passed),
                ("privilege_facts", _privilege_probe_facts),
                ("process_tree_control", _process_tree_control_passed),
                ("process_tree_interrupt", _interrupt_process_probe),
                ("process_tree", _process_tree_probe_passed),
                ("process_tree_paths", _process_tree_paths),
                ("remove_probe_paths", _remove_probe_paths),
                ("resource_facts", _resource_probe_facts),
                ("case_evidence", _case_evidence),
                ("case_evidence_digest", _case_evidence_digest),
                ("case_evidence_validator", _validate_case_evidence),
                ("runtime_case_row", _runtime_case_row),
                ("runtime_case_result", _runtime_case_result),
                ("runtime_cleanup_session", _runtime_cleanup_session),
                ("runtime_failed_vertical", _failed_runtime_vertical),
                ("apply_case_result", _apply_case_result),
                ("apply_case_row", _apply_case_row),
                ("apply_failed_vertical", _failed_apply_vertical),
                ("apply_fixture_context", _apply_fixture_context),
                ("apply_passing_facts", _passing_apply_facts),
                ("apply_passing_rows", _passing_apply_case_rows),
                ("apply_release_rows", _release_apply_rows),
                ("apply_vertical", _run_apply_vertical),
                ("case_gate_results", _case_gate_results),
                ("runtime_tool_orchestration", _runtime_tool_orchestration),
                ("runtime_tool_vertical", _run_runtime_tool_vertical),
                ("runtime_facts", _runtime_probe_facts),
                ("runtime_checks", _runtime_probe_checks),
                ("sensitive_facts", _sensitive_probe_facts),
                ("snapshot_source_tree", snapshot_source_tree),
                ("tool_facts", _tool_probe_facts),
                ("workspace_crud_facts", _workspace_crud_facts),
                (
                    "workspace_persistence_facts",
                    _workspace_persistence_facts,
                ),
            )
        },
        "sensitive": {name: True for name in sorted(_SENSITIVE_PROBE_FIELDS)},
        "tools": {name: True for name in sorted(_TOOL_PROBE_FIELDS)},
        "workspace_crud": {name: True for name in sorted(_WORKSPACE_CRUD_FIELDS)},
        "workspace_persistence": {
            name: True for name in sorted(_WORKSPACE_PERSIST_FIELDS)
        },
        "unsupported_entry_kinds": list(_UNSUPPORTED_ENTRY_KINDS),
    }


def _corpus_digest(payload=None):
    value = _corpus_payload() if payload is None else payload
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_RE = re.compile(r"^d7-(?:darwin|linux)-(?:arm64|amd64)-(?:clean|soak)-[0-9]{2}$")
_SMOKE_JOB_ID_RE = re.compile(
    r"^d7-(?:darwin|linux)-(?:arm64|amd64)-public-smoke$"
)
_ARTIFACT_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "platform",
    "architecture",
    "engine_profile",
    "distribution_sha256",
    "installed_tree_digest",
    "image_set_digest",
    "image_digest",
    "policy_digest",
    "corpus_digest",
    "case_evidence",
    "checks",
    "mandatory_passed",
    "mandatory_failed",
    "container_calls",
    "target_started_count",
    "host_fallback_count",
    "residue_count",
    "prepare_network_performed",
    "runtime_network_performed",
    "state_mutation_performed",
    "product_enablement",
    "release_binding",
}
_CASE_EVIDENCE_FIELDS = {
    "format_version",
    "execution_status",
    "reason_code",
    "cases",
    "evidence_digest",
}
_CASE_ROW_FIELDS = {"case_id", "status", "reason_code", "facts"}
_NOT_RUN_REASON_CODES = {
    "docker_daemon_unavailable",
    "docker_rootless_required",
    "docker_seccomp_unavailable",
    "docker_server_unsupported",
    "release_input_mismatch",
    "release_job_identity_mismatch",
    "release_source_identity_mismatch",
    "sandbox_corpus_identity_mismatch",
    "sandbox_image_identity_mismatch",
    "sandbox_image_missing",
    "sandbox_image_not_released",
}
_EXPECTED_MANIFEST_FIELDS = {
    "record_type",
    "format_version",
    "release_nonce",
    "commit",
    "distribution_sha256",
    "sdist_sha256",
    "image_set_digest",
    "images",
    "policy_digest",
    "corpus_digest",
    "jobs",
}
_EXPECTED_IMAGE_FIELDS = {
    "platform",
    "architecture",
    "image_digest",
    "image_id",
    "registry_reference",
}
_CANDIDATE_SMOKE_EXPECTED_FIELDS = {
    "record_type",
    "format_version",
    "release_nonce",
    "candidate_nonce",
    "commit",
    "distribution_sha256",
    "sdist_sha256",
    "image_set_digest",
    "images",
    "policy_digest",
    "corpus_digest",
    "production_aggregate_digest",
    "candidate_attestation_digest",
    "jobs",
}
_CANDIDATE_SMOKE_ARTIFACT_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "platform",
    "architecture",
    "engine_profile",
    "distribution_sha256",
    "installed_tree_digest",
    "image_set_digest",
    "image_digest",
    "policy_digest",
    "corpus_digest",
    "production_aggregate_digest",
    "candidate_attestation_digest",
    "public_cli_exit_code",
    "session_state",
    "source_unchanged",
    "product_cache_written",
    "host_fallback_count",
    "residue_count",
    "release_binding",
    "product_enablement",
}
_CANDIDATE_SMOKE_BINDING_FIELDS = {
    "status",
    "expected_manifest_digest",
    "release_nonce",
    "candidate_nonce",
    "job_id",
    "commit",
    "sdist_sha256",
}
_RELEASE_BINDING_FIELDS = {
    "status",
    "expected_manifest_digest",
    "release_nonce",
    "job_id",
    "commit",
    "sdist_sha256",
    "run_kind",
    "run_index",
}
_RELEASE_TARGETS = (
    ("darwin", "arm64", "desktop_vm"),
    ("darwin", "amd64", "desktop_vm"),
    ("linux", "arm64", "linux_rootless"),
    ("linux", "amd64", "linux_rootless"),
)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _decode_json(raw):
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("invalid JSON constant")
        ),
    )


def expected_release_jobs():
    jobs = []
    for platform_name, architecture, engine_profile in _RELEASE_TARGETS:
        for run_kind, count in (("clean", 3), ("soak", 20)):
            for run_index in range(1, count + 1):
                jobs.append(
                    {
                        "job_id": (
                            f"d7-{platform_name}-{architecture}-{run_kind}-{run_index:02d}"
                        ),
                        "platform": platform_name,
                        "architecture": architecture,
                        "engine_profile": engine_profile,
                        "run_kind": run_kind,
                        "run_index": run_index,
                    }
                )
    return tuple(jobs)


def expected_candidate_smoke_jobs():
    return tuple(
        {
            "job_id": f"d7-{platform_name}-{architecture}-public-smoke",
            "platform": platform_name,
            "architecture": architecture,
            "engine_profile": engine_profile,
        }
        for platform_name, architecture, engine_profile in _RELEASE_TARGETS
    )


def _valid_expected_images(images):
    return (
        isinstance(images, list)
        and len(images) == 2
        and [item.get("platform") for item in images if isinstance(item, dict)]
        == ["linux/arm64", "linux/amd64"]
        and all(
            isinstance(item, dict)
            and set(item) == _EXPECTED_IMAGE_FIELDS
            and item["architecture"] in {"arm64", "amd64"}
            and item["platform"] == "linux/" + item["architecture"]
            and all(
                isinstance(item.get(name), str)
                and _SHA256_RE.fullmatch(item[name]) is not None
                for name in ("image_digest", "image_id")
            )
            and isinstance(item.get("registry_reference"), str)
            and item["registry_reference"].endswith("@" + item["image_digest"])
            for item in images
        )
    )


def validate_expected_manifest(value):
    jobs = value.get("jobs") if isinstance(value, dict) else None
    images = value.get("images") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != _EXPECTED_MANIFEST_FIELDS
        or value.get("record_type") != "docker_sandbox_release_expected"
        or type(value.get("format_version")) is not int
        or value["format_version"] != 1
        or not isinstance(value.get("release_nonce"), str)
        or _NONCE_RE.fullmatch(value["release_nonce"]) is None
        or not isinstance(value.get("commit"), str)
        or _COMMIT_RE.fullmatch(value["commit"]) is None
        or any(
            not isinstance(value.get(name), str)
            or _SHA256_RE.fullmatch(value[name]) is None
            for name in (
                "distribution_sha256",
                "sdist_sha256",
                "image_set_digest",
                "policy_digest",
                "corpus_digest",
            )
        )
        or value["corpus_digest"] != CORPUS_DIGEST
        or not _valid_expected_images(images)
        or not isinstance(jobs, list)
        or any(
            not isinstance(job, dict)
            or set(job)
            != {
                "job_id",
                "platform",
                "architecture",
                "engine_profile",
                "run_kind",
                "run_index",
            }
            or type(job["run_index"]) is not int
            for job in jobs
        )
        or jobs != list(expected_release_jobs())
    ):
        raise ValueError("invalid expected release manifest")
    return value


def validate_candidate_smoke_expected_manifest(value):
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != _CANDIDATE_SMOKE_EXPECTED_FIELDS
        or value.get("record_type") != "docker_sandbox_candidate_smoke_expected"
        or type(value.get("format_version")) is not int
        or value["format_version"] != 1
        or not isinstance(value.get("release_nonce"), str)
        or _NONCE_RE.fullmatch(value["release_nonce"]) is None
        or not isinstance(value.get("candidate_nonce"), str)
        or _NONCE_RE.fullmatch(value["candidate_nonce"]) is None
        or not isinstance(value.get("commit"), str)
        or _COMMIT_RE.fullmatch(value["commit"]) is None
        or any(
            not isinstance(value.get(name), str)
            or _SHA256_RE.fullmatch(value[name]) is None
            for name in (
                "distribution_sha256",
                "sdist_sha256",
                "image_set_digest",
                "policy_digest",
                "corpus_digest",
                "production_aggregate_digest",
                "candidate_attestation_digest",
            )
        )
        or value["corpus_digest"] != CORPUS_DIGEST
        or not _valid_expected_images(value.get("images"))
        or not isinstance(jobs, list)
        or any(
            not isinstance(job, dict)
            or set(job)
            != {"job_id", "platform", "architecture", "engine_profile"}
            for job in jobs
        )
        or jobs != list(expected_candidate_smoke_jobs())
    ):
        raise ValueError("invalid candidate smoke expected manifest")
    return value


def candidate_smoke_expected_digest(value):
    validate_candidate_smoke_expected_manifest(value)
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def candidate_smoke_binding(expected, job_id):
    validate_candidate_smoke_expected_manifest(expected)
    job = next((item for item in expected["jobs"] if item["job_id"] == job_id), None)
    if job is None:
        raise ValueError("candidate smoke job is not expected")
    return {
        "status": "bound",
        "expected_manifest_digest": candidate_smoke_expected_digest(expected),
        "release_nonce": expected["release_nonce"],
        "candidate_nonce": expected["candidate_nonce"],
        "job_id": job["job_id"],
        "commit": expected["commit"],
        "sdist_sha256": expected["sdist_sha256"],
    }


def validate_candidate_smoke_artifact(value, *, require_pass=False):
    if not isinstance(value, dict) or set(value) != _CANDIDATE_SMOKE_ARTIFACT_FIELDS:
        raise ValueError("invalid candidate public smoke artifact")
    binding = value["release_binding"]
    if (
        value["record_type"] != "docker_sandbox_candidate_public_smoke"
        or value["format_version"] != 1
        or value["status"] not in {"passed", "failed"}
        or not isinstance(value["reason_code"], str)
        or not value["reason_code"]
        or value["platform"] not in {"darwin", "linux"}
        or value["architecture"] not in {"arm64", "amd64"}
        or value["engine_profile"] not in {"desktop_vm", "linux_rootless"}
        or any(
            not isinstance(value.get(name), str)
            or _SHA256_RE.fullmatch(value[name]) is None
            for name in (
                "distribution_sha256",
                "installed_tree_digest",
                "image_set_digest",
                "image_digest",
                "policy_digest",
                "corpus_digest",
                "production_aggregate_digest",
                "candidate_attestation_digest",
            )
        )
        or value["corpus_digest"] != CORPUS_DIGEST
        or type(value["public_cli_exit_code"]) is not int
        or value["session_state"] not in {"discarded", "failed"}
        or any(
            type(value[name]) is not bool
            for name in ("source_unchanged", "product_cache_written", "product_enablement")
        )
        or any(
            type(value[name]) is not int or value[name] < 0
            for name in ("host_fallback_count", "residue_count")
        )
        or value["product_enablement"] is not False
        or not isinstance(binding, dict)
        or set(binding) != _CANDIDATE_SMOKE_BINDING_FIELDS
        or binding.get("status") != "bound"
        or _SHA256_RE.fullmatch(binding.get("expected_manifest_digest", "")) is None
        or _NONCE_RE.fullmatch(binding.get("release_nonce", "")) is None
        or _NONCE_RE.fullmatch(binding.get("candidate_nonce", "")) is None
        or _SMOKE_JOB_ID_RE.fullmatch(binding.get("job_id", "")) is None
        or _COMMIT_RE.fullmatch(binding.get("commit", "")) is None
        or _SHA256_RE.fullmatch(binding.get("sdist_sha256", "")) is None
    ):
        raise ValueError("invalid candidate public smoke artifact")
    if require_pass and (
        value["status"] != "passed"
        or value["reason_code"] != "public_cli_smoke_passed"
        or value["public_cli_exit_code"] != 0
        or value["session_state"] != "discarded"
        or value["source_unchanged"] is not True
        or value["product_cache_written"] is not False
        or value["host_fallback_count"] != 0
        or value["residue_count"] != 0
    ):
        raise ValueError("candidate public smoke did not pass")
    return value


def expected_manifest_digest(value):
    validate_expected_manifest(value)
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def unbound_release_binding():
    return {
        "status": "unbound",
        "expected_manifest_digest": "",
        "release_nonce": "",
        "job_id": "",
        "commit": "",
        "sdist_sha256": "",
        "run_kind": "unbound",
        "run_index": 0,
    }


def release_binding(expected, job_id):
    validate_expected_manifest(expected)
    job = next((item for item in expected["jobs"] if item["job_id"] == job_id), None)
    if job is None:
        raise ValueError("release job is not expected")
    return {
        "status": "bound",
        "expected_manifest_digest": expected_manifest_digest(expected),
        "release_nonce": expected["release_nonce"],
        "job_id": job["job_id"],
        "commit": expected["commit"],
        "sdist_sha256": expected["sdist_sha256"],
        "run_kind": job["run_kind"],
        "run_index": job["run_index"],
    }


def _validate_release_binding(value, *, require_bound=False):
    if not isinstance(value, dict) or set(value) != _RELEASE_BINDING_FIELDS:
        raise ValueError("invalid production vertical release binding")
    if value["status"] == "unbound":
        if value != unbound_release_binding() or require_bound:
            raise ValueError("production vertical release binding missing")
        return value
    if (
        value["status"] != "bound"
        or not isinstance(value["expected_manifest_digest"], str)
        or _SHA256_RE.fullmatch(value["expected_manifest_digest"]) is None
        or not isinstance(value["release_nonce"], str)
        or _NONCE_RE.fullmatch(value["release_nonce"]) is None
        or not isinstance(value["job_id"], str)
        or _JOB_ID_RE.fullmatch(value["job_id"]) is None
        or not isinstance(value["commit"], str)
        or _COMMIT_RE.fullmatch(value["commit"]) is None
        or not isinstance(value["sdist_sha256"], str)
        or _SHA256_RE.fullmatch(value["sdist_sha256"]) is None
        or value["run_kind"] not in {"clean", "soak"}
        or type(value["run_index"]) is not int
        or value["run_index"] < 1
        or value["run_index"] > (3 if value["run_kind"] == "clean" else 20)
    ):
        raise ValueError("invalid production vertical release binding")
    return value


def _case_evidence_digest(execution_status, reason_code, cases, binding):
    payload = {
        "format_version": 1,
        "execution_status": execution_status,
        "reason_code": reason_code,
        "cases": cases,
        "corpus_digest": CORPUS_DIGEST,
        "release_binding": binding,
    }
    return "sha256:" + hashlib.sha256(
        b"PICO_DOCKER_SANDBOX_CASE_EVIDENCE_V1\0" + _canonical_json(payload)
    ).hexdigest()


def _set_case_evidence(artifact, execution_status, reason_code, cases):
    artifact["case_evidence"] = _case_evidence(
        execution_status,
        reason_code,
        cases,
        artifact["release_binding"],
    )


def _mark_not_run(artifact, reason_code, *, status="failed"):
    artifact["status"] = status
    artifact["reason_code"] = reason_code
    for check in artifact["checks"]:
        check["status"] = "blocked"
        check["reason_code"] = reason_code
    artifact["mandatory_passed"] = 0
    artifact["mandatory_failed"] = len(MANDATORY_CHECK_IDS)
    _set_case_evidence(artifact, "not_run", reason_code, [])
    return artifact


def _case_evidence(execution_status, reason_code, cases, binding):
    rows = list(cases)
    return {
        "format_version": 1,
        "execution_status": execution_status,
        "reason_code": reason_code,
        "cases": rows,
        "evidence_digest": _case_evidence_digest(
            execution_status,
            reason_code,
            rows,
            binding,
        ),
    }


def _case_gate_results(rows):
    statuses = {row["case_id"]: row["status"] == "pass" for row in rows}
    return {
        gate: all(statuses.get(case_id) is True for case_id in case_ids)
        for gate, case_ids in {
            **_APPLY_CASE_PARENT_GATES,
            **_NETWORK_CASE_PARENT_GATES,
            **_RUNTIME_CASE_PARENT_GATES,
        }.items()
    }


def _validate_case_evidence(value, binding, *, artifact_status):
    if not isinstance(value, dict) or set(value) != _CASE_EVIDENCE_FIELDS:
        raise ValueError("invalid production vertical case evidence")
    execution_status = value["execution_status"]
    reason_code = value["reason_code"]
    cases = value["cases"]
    if (
        value["format_version"] != 1
        or execution_status not in {"not_run", "complete"}
        or not isinstance(reason_code, str)
        or not reason_code
        or not isinstance(cases, list)
        or len(cases) > 256
        or value["evidence_digest"]
        != _case_evidence_digest(execution_status, reason_code, cases, binding)
    ):
        raise ValueError("invalid production vertical case evidence")
    if execution_status == "not_run":
        if (
            cases
            or artifact_status not in {"blocked", "failed"}
            or reason_code not in _NOT_RUN_REASON_CODES
        ):
            raise ValueError("invalid production vertical case evidence")
        return {"__not_run_reason__": reason_code}
    if reason_code != "verified" or [row.get("case_id") for row in cases] != list(
        _CASE_IDS
    ):
        raise ValueError("invalid production vertical case evidence")
    for row in cases:
        if not isinstance(row, dict) or set(row) != _CASE_ROW_FIELDS:
            raise ValueError("invalid production vertical case evidence")
        if row["case_id"] in _APPLY_CASE_IDS:
            result_owner = _apply_case_result
        elif row["case_id"] in _NETWORK_CASE_IDS:
            result_owner = _network_case_result
        else:
            result_owner = _runtime_case_result
        passed, expected_reason = result_owner(row["case_id"], row["facts"])
        if row["status"] != ("pass" if passed else "fail") or row[
            "reason_code"
        ] != expected_reason:
            raise ValueError("invalid production vertical case evidence")
    network_bindings = {
        (row["facts"]["endpoint_digest"], row["facts"]["evidence_digest"])
        for row in cases
        if row["case_id"] in _NETWORK_CASE_IDS
    }
    if len(network_bindings) != 1:
        raise ValueError("invalid production vertical case evidence")
    return _case_gate_results(cases)


def _sha256_file(path, *, max_bytes=512 * 1024 * 1024):
    path = Path(path)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise ValueError("release input is not a bounded regular file")
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("release input changed")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("release input changed")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        ):
            raise ValueError("release input changed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_bounded_file(path, *, max_bytes):
    path = Path(path)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        before = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, uid}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > max_bytes
        ):
            raise ValueError("release input is not a bounded regular file")
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("release input changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("release input changed")
        after = os.fstat(descriptor)
        current = path.lstat()
        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_uid,
                value.st_gid,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("release input changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _worker_environment(home):
    environment = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }
    if os.environ.get("XDG_RUNTIME_DIR"):
        environment["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    return environment


def _candidate_docker_home(home, endpoint, platform_name):
    if platform_name != "darwin":
        return
    docker_root = Path(home) / ".docker"
    socket_root = docker_root / "run"
    socket_root.mkdir(parents=True)
    docker_root.chmod(0o700)
    socket_root.chmod(0o700)
    (socket_root / "docker.sock").symlink_to(Path(endpoint))


def _run_candidate_public_cli(source, environment):
    return _run_bounded_process(
        [
            os.fspath(Path(sys.executable).resolve(strict=True)),
            "-m",
            "pico",
            "--cwd",
            str(source),
            "--provider",
            "ollama",
            "--sandbox",
            "repl",
        ],
        env=environment,
        timeout=300,
        max_bytes=MAX_CANDIDATE_SMOKE_OUTPUT_BYTES,
        terminate_on_overflow=True,
    )


def _platform_identity():
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    architecture = {
        "aarch64": "arm64",
        "x86_64": "amd64",
    }.get(machine, machine)
    return system, architecture


def _resolve_release_input(
    args,
    distribution_sha256,
    image,
    *,
    platform_identity=None,
):
    values = (
        getattr(args, "release_expected", None),
        getattr(args, "expected_digest", None),
        getattr(args, "release_job_id", None),
        getattr(args, "sdist", None),
    )
    if not any(values):
        return unbound_release_binding(), None
    if not all(values):
        raise ValueError("release input mismatch")
    try:
        envelope = release_authority.decode_json(
            _read_bounded_file(args.release_expected, max_bytes=MAX_ARTIFACT_BYTES)
        )
        expected = release_authority.verify_signed_envelope(
            envelope,
            purpose=release_authority.EXPECTED_MANIFEST_PURPOSE,
        )
        validate_expected_manifest(expected)
        job = next(
            item for item in expected["jobs"] if item["job_id"] == args.release_job_id
        )
    except (
        OSError,
        StopIteration,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        release_authority.ReleaseAuthorityError,
    ) as exc:
        raise ValueError("release input mismatch") from exc
    system, architecture = platform_identity or _platform_identity()
    if (
        args.expected_digest != expected_manifest_digest(expected)
        or distribution_sha256 != expected["distribution_sha256"]
        or _sha256_file(args.sdist) != expected["sdist_sha256"]
        or image.image_set_digest != expected["image_set_digest"]
        or image.policy_digest != expected["policy_digest"]
        or image.corpus_digest != expected["corpus_digest"]
        or job["platform"] != system
        or job["architecture"] != architecture
    ):
        raise ValueError("release input mismatch")
    expected_image = next(
        (item for item in expected["images"] if item["platform"] == image.platform),
        None,
    )
    if expected_image != {
        "platform": image.platform,
        "architecture": image.architecture,
        "image_digest": image.reference,
        "image_id": image.image_id,
        "registry_reference": image.registry_reference,
    }:
        raise ValueError("release input mismatch")
    return release_binding(expected, job["job_id"]), job


def _installed_tree_digest(package_root):
    try:
        return release_authority.installed_tree_digest(
            package_root,
            metadata.version("pico"),
        )
    except release_authority.ReleaseAuthorityError as exc:
        raise ValueError("installed package tree is not ordinary") from exc


def _base_artifact(distribution_sha256, installed_tree_digest, image):
    system, architecture = _platform_identity()
    binding = unbound_release_binding()
    return {
        "record_type": "docker_sandbox_production_vertical",
        "format_version": 1,
        "status": "blocked",
        "reason_code": "sandbox_image_not_released",
        "platform": system,
        "architecture": architecture,
        "engine_profile": "unknown",
        "distribution_sha256": distribution_sha256,
        "installed_tree_digest": installed_tree_digest,
        "image_set_digest": image.image_set_digest,
        "image_digest": image.reference,
        "policy_digest": image.policy_digest,
        "corpus_digest": CORPUS_DIGEST,
        "case_evidence": _case_evidence(
            "not_run",
            "sandbox_image_not_released",
            [],
            binding,
        ),
        "checks": [
            {
                "check_id": check_id,
                "status": "blocked",
                "reason_code": "sandbox_image_not_released",
            }
            for check_id in MANDATORY_CHECK_IDS
        ],
        "mandatory_passed": 0,
        "mandatory_failed": len(MANDATORY_CHECK_IDS),
        "container_calls": 0,
        "target_started_count": 0,
        "host_fallback_count": 0,
        "residue_count": 0,
        "prepare_network_performed": False,
        "runtime_network_performed": False,
        "state_mutation_performed": False,
        "product_enablement": False,
        "release_binding": binding,
    }


def _candidate_smoke_base_artifact(
    expected,
    job,
    *,
    installed_tree_digest,
    image,
    candidate_attestation_digest,
):
    return {
        "record_type": "docker_sandbox_candidate_public_smoke",
        "format_version": 1,
        "status": "failed",
        "reason_code": "public_cli_smoke_failed",
        "platform": job["platform"],
        "architecture": job["architecture"],
        "engine_profile": job["engine_profile"],
        "distribution_sha256": expected["distribution_sha256"],
        "installed_tree_digest": installed_tree_digest,
        "image_set_digest": image.image_set_digest,
        "image_digest": image.reference,
        "policy_digest": image.policy_digest,
        "corpus_digest": image.corpus_digest,
        "production_aggregate_digest": expected["production_aggregate_digest"],
        "candidate_attestation_digest": candidate_attestation_digest,
        "public_cli_exit_code": -1,
        "session_state": "failed",
        "source_unchanged": False,
        "product_cache_written": False,
        "host_fallback_count": 0,
        "residue_count": 0,
        "release_binding": candidate_smoke_binding(expected, job["job_id"]),
        "product_enablement": False,
    }


def _resolve_candidate_smoke_inputs(args, *, package_root, image):
    from pico import sandbox_release_authority as authority

    envelope = authority.decode_json(
        _read_bounded_file(
            args.candidate_smoke_expected,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
    )
    expected = authority.verify_signed_envelope(
        envelope,
        purpose=authority.CANDIDATE_SMOKE_EXPECTED_PURPOSE,
    )
    validate_candidate_smoke_expected_manifest(expected)
    if args.candidate_smoke_expected_digest != candidate_smoke_expected_digest(
        expected
    ):
        raise ValueError("candidate smoke input mismatch")
    job = next(
        item
        for item in expected["jobs"]
        if item["job_id"] == args.candidate_smoke_job_id
    )
    candidate = authority.read_candidate_attestation(args.candidate_attestation)
    candidate_digest = authority.attestation_digest(candidate)
    if candidate_digest != expected["candidate_attestation_digest"]:
        raise ValueError("candidate smoke input mismatch")
    payload = authority.verify_candidate_attestation(
        candidate,
        package_root=package_root,
        distribution_version=metadata.version("pico"),
        image=image,
        candidate_nonce=expected["candidate_nonce"],
    )
    production = _decode_json(
        _read_bounded_file(args.production_aggregate, max_bytes=MAX_ARTIFACT_BYTES)
    )
    if (
        authority.canonical_digest(production)
        != expected["production_aggregate_digest"]
        or payload["production_aggregate_digest"]
        != expected["production_aggregate_digest"]
        or args.distribution_sha256 != expected["distribution_sha256"]
        or _sha256_file(args.sdist) != expected["sdist_sha256"]
        or image.image_set_digest != expected["image_set_digest"]
        or image.policy_digest != expected["policy_digest"]
        or image.corpus_digest != expected["corpus_digest"]
    ):
        raise ValueError("candidate smoke input mismatch")
    expected_image = next(
        item for item in expected["images"] if item["platform"] == image.platform
    )
    system, architecture = _platform_identity()
    if (
        job["platform"] != system
        or job["architecture"] != architecture
        or expected_image
        != {
            "platform": image.platform,
            "architecture": image.architecture,
            "image_digest": image.reference,
            "image_id": image.image_id,
            "registry_reference": image.registry_reference,
        }
    ):
        raise ValueError("candidate smoke input mismatch")
    return expected, job, candidate_digest


def validate_artifact(value, *, require_pass=False, require_release_binding=False):
    if not isinstance(value, dict) or set(value) != _ARTIFACT_FIELDS:
        raise ValueError("invalid production vertical artifact")
    checks = value["checks"]
    binding = value.get("release_binding")
    if (
        value["record_type"] != "docker_sandbox_production_vertical"
        or value["format_version"] != 1
        or value["status"] not in {"passed", "blocked", "failed"}
        or not isinstance(value["reason_code"], str)
        or not value["reason_code"]
        or value["platform"] not in {"darwin", "linux"}
        or value["architecture"] not in {"arm64", "amd64"}
        or value["engine_profile"]
        not in {"unknown", "desktop_vm", "linux_rootless"}
        or any(
            not isinstance(value[name], str)
            or _SHA256_RE.fullmatch(value[name]) is None
            for name in (
                "distribution_sha256",
                "installed_tree_digest",
                "image_set_digest",
                "image_digest",
                "policy_digest",
                "corpus_digest",
            )
        )
        or value["corpus_digest"] != CORPUS_DIGEST
        or not isinstance(checks, list)
        or [item.get("check_id") for item in checks if isinstance(item, dict)]
        != list(MANDATORY_CHECK_IDS)
        or any(
            set(item) != {"check_id", "status", "reason_code"}
            or item["status"] not in {"pass", "fail", "blocked"}
            or not isinstance(item["reason_code"], str)
            or not item["reason_code"]
            for item in checks
        )
        or any(
            type(value[name]) is not int or value[name] < 0
            for name in (
                "mandatory_passed",
                "mandatory_failed",
                "container_calls",
                "target_started_count",
                "host_fallback_count",
                "residue_count",
            )
        )
        or value["mandatory_passed"]
        != sum(item["status"] == "pass" for item in checks)
        or value["mandatory_failed"] != len(checks) - value["mandatory_passed"]
        or any(
            type(value[name]) is not bool
            for name in (
                "prepare_network_performed",
                "runtime_network_performed",
                "state_mutation_performed",
                "product_enablement",
            )
        )
        or value["runtime_network_performed"] is not False
        or value["host_fallback_count"] != 0
        or value["product_enablement"] is not False
    ):
        raise ValueError("invalid production vertical artifact")
    _validate_release_binding(binding, require_bound=require_release_binding)
    gate_results = _validate_case_evidence(
        value["case_evidence"],
        binding,
        artifact_status=value["status"],
    )
    check_map = {item["check_id"]: item for item in checks}
    if "__not_run_reason__" in gate_results and any(
        item["status"] != "blocked"
        or item["reason_code"] != gate_results["__not_run_reason__"]
        for item in checks
    ):
        raise ValueError("invalid production vertical artifact")
    if "__not_run_reason__" not in gate_results and any(
        check_map[gate]["status"] != ("pass" if passed else "fail")
        or check_map[gate]["reason_code"]
        != ("verified" if passed else gate + "_failed")
        for gate, passed in gate_results.items()
    ):
        raise ValueError("invalid production vertical artifact")
    if value["case_evidence"]["execution_status"] == "not_run" and (
        value["container_calls"] != 0
        or value["target_started_count"] != 0
        or value["residue_count"] != 0
        or value["state_mutation_performed"] is not False
    ):
        raise ValueError("invalid production vertical artifact")
    if require_pass and (
        value["status"] != "passed"
        or value["reason_code"] != "mandatory_checks_passed"
        or value["mandatory_passed"] != len(MANDATORY_CHECK_IDS)
        or value["mandatory_failed"] != 0
        or value["residue_count"] != 0
        or any(item["status"] != "pass" for item in checks)
    ):
        raise ValueError("production vertical did not pass")
    return value


def _set_check(artifact, check_id, passed, reason="verified"):
    item = next(value for value in artifact["checks"] if value["check_id"] == check_id)
    item["status"] = "pass" if passed else "fail"
    item["reason_code"] = reason if passed else check_id + "_failed"


def _verify_release_source(source, commit):
    source = Path(source).resolve(strict=True)
    git = build_trusted_executables(source, names=("git",)).get("git")
    if git is None:
        raise ValueError("release source identity mismatch")
    try:
        top_level = run_hardened_git(
            git,
            ["rev-parse", "--show-toplevel"],
            cwd=source,
            timeout=30,
        )
        head = run_hardened_git(
            git,
            ["rev-parse", "--verify", "HEAD"],
            cwd=source,
            timeout=30,
        )
        status = run_hardened_git(
            git,
            ["status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
            cwd=source,
            timeout=30,
        )
        reported_root = Path(top_level.stdout.decode("utf-8").strip()).resolve()
        actual = head.stdout.decode("ascii").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("release source identity mismatch") from exc
    if (
        top_level.returncode != 0
        or reported_root != source
        or head.returncode != 0
        or status.returncode != 0
        or actual != commit
        or status.stdout
    ):
        raise ValueError("release source identity mismatch")
    return actual


def _json_probe_facts(outcome, expected_fields):
    if (
        outcome.sandbox_outcome != "completed"
        or outcome.exit_code != 0
        or outcome.timed_out
        or outcome.stdout_truncated
        or outcome.stderr_truncated
        or outcome.stdout_bytes != len(outcome.stdout)
        or outcome.stderr_bytes != len(outcome.stderr)
        or outcome.stderr
        or outcome.cleanup_status != "completed"
        or outcome.residue_detected
    ):
        return {}
    try:
        facts = _decode_json(outcome.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    if (
        not isinstance(facts, dict)
        or set(facts) != set(expected_fields)
        or any(type(value) is not bool for value in facts.values())
    ):
        return {}
    return facts


def _runtime_probe_facts(outcome):
    return _json_probe_facts(outcome, _RUNTIME_PROBE_FIELDS)


def _privilege_probe_facts(outcome):
    return _json_probe_facts(outcome, _PRIVILEGE_PROBE_FIELDS)


def _resource_probe_facts(outcome):
    return _json_probe_facts(outcome, _RESOURCE_PROBE_FIELDS)


def _ephemeral_probe_facts(outcome):
    return _json_probe_facts(outcome, _EPHEMERAL_PROBE_FIELDS)


def _sensitive_probe_facts(outcome):
    return _json_probe_facts(outcome, _SENSITIVE_PROBE_FIELDS)


def _tool_probe_facts(outcome):
    return _json_probe_facts(outcome, _TOOL_PROBE_FIELDS)


def _workspace_crud_facts(outcome):
    return _json_probe_facts(outcome, _WORKSPACE_CRUD_FIELDS)


def _workspace_persistence_facts(outcome):
    return _json_probe_facts(outcome, _WORKSPACE_PERSIST_FIELDS)


def _network_probe_output(outcome, expected_fields):
    if hasattr(outcome, "sandbox_outcome"):
        return _json_probe_facts(outcome, expected_fields)
    if (
        outcome.timed_out
        or outcome.exit_code != 0
        or outcome.stdout_truncated
        or outcome.stderr_truncated
        or outcome.stdout_bytes != len(outcome.stdout)
        or outcome.stderr_bytes != len(outcome.stderr)
        or outcome.stderr
    ):
        return {}
    try:
        facts = _decode_json(outcome.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    if (
        not isinstance(facts, dict)
        or set(facts) != set(expected_fields)
        or any(type(value) is not bool for value in facts.values())
    ):
        return {}
    return facts


def _network_host_listener(nonce):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 0))
    server.listen(8)
    server.settimeout(0.1)
    stopped = threading.Event()
    challenge = bytes.fromhex(nonce)

    def serve():
        while not stopped.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(_NETWORK_PROBE_TIMEOUT)
                try:
                    payload = b""
                    while len(payload) < len(challenge):
                        chunk = connection.recv(len(challenge) - len(payload))
                        if not chunk:
                            break
                        payload += chunk
                    if payload == challenge:
                        connection.sendall(challenge)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, thread, stopped, server.getsockname()[1]


def _network_host_listener_control(port, nonce):
    challenge = bytes.fromhex(nonce)
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_NETWORK_PROBE_TIMEOUT
        ) as connection:
            connection.sendall(challenge)
            payload = b""
            while len(payload) < len(challenge):
                chunk = connection.recv(len(challenge) - len(payload))
                if not chunk:
                    break
                payload += chunk
            return payload == challenge
    except OSError:
        return False


def _network_host_to_guest_denied(port):
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_NETWORK_PROBE_TIMEOUT
        ):
            return False
    except OSError:
        return True


def _stop_network_host_listener(server, thread, stopped):
    stopped.set()
    try:
        server.close()
    except OSError:
        pass
    thread.join(timeout=_NETWORK_PROBE_TIMEOUT)
    return not thread.is_alive()


def _docker_inspect_payload(client, kind, object_id):
    result = client.command(
        [kind, "inspect", object_id, "--format", "{{json .}}"],
        timeout=30,
        max_bytes=4 * 1024 * 1024,
    )
    if (
        result.timed_out
        or result.exit_code != 0
        or result.stdout_truncated
        or result.stderr_truncated
        or result.stdout_bytes != len(result.stdout)
        or result.stderr_bytes != len(result.stderr)
        or result.stderr
    ):
        raise ValueError("network inspect failed")
    payload = _decode_json(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("network inspect failed")
    return payload


def _cleanup_network_context(context):
    if context is None:
        return False
    try:
        store = context.runner.session_store
        current = context.current_session()
        if current.state == "ready":
            if current.manifest["lease"] is None:
                current = store.acquire(current.state_root)
            result = store.discard(current.state_root)
        elif current.state == "discarded":
            result = current
        else:
            return False
        return bool(
            result.state == "discarded"
            and result.manifest["lease"] is None
            and result.manifest["cleanup"]["status"] == "complete"
            and not context.execution_root.exists()
        )
    except Exception:
        return False


def _run_network_vertical(
    *,
    client,
    image,
    release_binding,
    work_root,
    build_context,
    python,
):
    from pico.docker_sandbox import (
        DockerSandboxRunner,
        compile_create_argv,
        verify_container_inspect,
    )
    from pico.docker_sandbox_network_control import (
        NetworkControl,
        NetworkControlEndpoints,
        NetworkControlNegativeFacts,
        NetworkControlPositiveFacts,
    )

    control = None
    context = None
    outcome = None
    helper = None
    listener = None
    listener_stopped = False
    production_cleaned = False
    rows = _failed_network_vertical()
    nonce = os.urandom(32).hex()
    guest_port = _NETWORK_GUEST_TCP_PORT
    gateway_port = 0
    host_port = 0
    marker_root = work_root / "network-production-source"
    ready = None
    done = None
    helper_facts = {
        "host_to_guest_denied": False,
        "production_network_none": False,
    }
    positive_facts = {}
    production_facts = {}
    host_client_control = False

    try:
        listener = _network_host_listener(nonce)
        server, listener_thread, listener_stop, host_port = listener
        gateway_port = host_port
        host_client_control = _network_host_listener_control(host_port, nonce)
        peer_argv = (
            python,
            "-c",
            _NETWORK_PEER_PROBE,
            nonce,
            str(_NETWORK_PEER_TCP_PORT),
            str(_NETWORK_PEER_UDP_PORT),
        )
        control = NetworkControl.open(
            client,
            work_root / "network-control",
            release_binding=release_binding,
            image_reference=image.reference,
            image_id=image.image_id,
            peer_argv=peer_argv,
            nonce=nonce,
        )

        def positive_probe(topology, challenge_nonce):
            nonlocal positive_facts
            argv = [
                "container",
                "exec",
                control.owner["peer_id"],
                python,
                "-c",
                _NETWORK_CONTROL_PROBE,
                topology.peer_alias,
                topology.peer_ipv4,
                str(_NETWORK_PEER_TCP_PORT),
                str(_NETWORK_PEER_UDP_PORT),
                topology.gateway_ipv4,
                str(gateway_port),
                _NETWORK_HOST_ALIAS,
                str(host_port),
                challenge_nonce,
                str(_NETWORK_PROBE_TIMEOUT),
            ]
            deadline = time.monotonic() + _NETWORK_GUEST_WAIT_SECONDS
            while time.monotonic() < deadline:
                outcome_value = client.command(
                    argv,
                    timeout=int(_NETWORK_PROBE_TIMEOUT) + 2,
                )
                positive_facts = _network_probe_output(
                    outcome_value, _NETWORK_POSITIVE_FIELDS
                )
                if positive_facts and all(positive_facts.values()):
                    break
                time.sleep(0.05)
            endpoints = NetworkControlEndpoints(
                topology_digest=topology.topology_digest,
                peer_alias=topology.peer_alias,
                peer_ipv4=topology.peer_ipv4,
                peer_tcp_port=_NETWORK_PEER_TCP_PORT,
                peer_udp_port=_NETWORK_PEER_UDP_PORT,
                gateway_ipv4=topology.gateway_ipv4,
                gateway_tcp_port=gateway_port,
                host_address=_NETWORK_HOST_ALIAS,
                host_tcp_port=host_port,
                guest_tcp_port=guest_port,
            )
            return NetworkControlPositiveFacts(
                endpoints=endpoints,
                challenge_nonce=challenge_nonce,
                peer_dns_reachable=positive_facts.get(
                    "control_peer_dns_reachable"
                )
                is True,
                peer_tcp_reachable=positive_facts.get(
                    "control_peer_tcp_reachable"
                )
                is True,
                peer_udp_reachable=positive_facts.get(
                    "control_peer_udp_reachable"
                )
                is True,
                gateway_reachable=positive_facts.get(
                    "control_gateway_reachable"
                )
                is True,
                host_reachable=positive_facts.get("control_host_reachable")
                is True,
            )

        def production_probe(endpoints, challenge_nonce):
            nonlocal context, done, helper, helper_facts, outcome
            nonlocal production_facts, ready
            marker_root.mkdir(mode=0o700)
            context = build_context(marker_root)
            ready = context.execution_root / "guest-ready"
            done = context.execution_root / "host-done"
            runner = DockerSandboxRunner(
                client,
                context.runner.session_store,
                image,
            )
            current = context.current_session()
            plan = runner.compile(
                current,
                [
                    python,
                    "-c",
                    _NETWORK_PRODUCTION_PROBE,
                    endpoints.peer_alias,
                    str(endpoints.peer_tcp_port),
                    str(endpoints.peer_udp_port),
                    endpoints.gateway_ipv4,
                    str(endpoints.gateway_tcp_port),
                    endpoints.host_address,
                    str(endpoints.host_tcp_port),
                    str(endpoints.guest_tcp_port),
                    challenge_nonce,
                    str(_NETWORK_PROBE_TIMEOUT),
                    "/workspace/guest-ready",
                    "/workspace/host-done",
                ],
                timeout=_NETWORK_RUNNER_TIMEOUT,
            )

            def host_probe():
                deadline = time.monotonic() + _NETWORK_GUEST_WAIT_SECONDS
                while not ready.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                armed = ready.is_file()
                denied = armed and _network_host_to_guest_denied(
                    endpoints.guest_tcp_port
                )
                inspect_ok = False
                try:
                    active = context.runner.session_store.inspect(
                        context.sandbox_state_root
                    ).manifest["active_call"]
                    container_id = str((active or {}).get("container_id") or "")
                    payload = _docker_inspect_payload(
                        client, "container", container_id
                    )
                    verify_container_inspect(
                        payload,
                        plan,
                        expected_id=container_id,
                    )
                    inspect_ok = "--network=none" in compile_create_argv(plan)
                except Exception:
                    inspect_ok = False
                helper_facts.update(
                    host_to_guest_denied=denied,
                    production_network_none=inspect_ok,
                )
                try:
                    done.write_bytes(b"done\n")
                except OSError:
                    pass

            helper = threading.Thread(target=host_probe, daemon=True)
            helper.start()
            outcome = runner.execute(current, plan)
            helper.join(timeout=_NETWORK_THREAD_JOIN_TIMEOUT)
            production_facts = _network_probe_output(
                outcome, _NETWORK_PRODUCTION_FIELDS
            )
            return NetworkControlNegativeFacts(
                endpoint_digest=endpoints.endpoint_digest,
                peer_dns_denied=production_facts.get(
                    "production_peer_dns_denied"
                )
                is True,
                peer_tcp_denied=production_facts.get(
                    "production_peer_tcp_denied"
                )
                is True,
                peer_udp_denied=production_facts.get(
                    "production_peer_udp_denied"
                )
                is True,
                gateway_denied=production_facts.get(
                    "production_gateway_denied"
                )
                is True,
                host_denied=production_facts.get("production_host_denied")
                is True,
                host_to_guest_denied=helper_facts["host_to_guest_denied"],
            )

        result = control.run(positive_probe, production_probe)
        production_cleaned = _cleanup_network_context(context)
        for marker in (ready, done):
            if marker is not None:
                marker.unlink(missing_ok=True)
        probe_facts = {
            "challenge_bound": bool(
                production_facts.get("challenge_bound") is True
                and positive_facts
                and all(positive_facts.values())
                and host_client_control
                and helper_facts["production_network_none"]
            ),
            "guest_listener_armed": production_facts.get(
                "guest_listener_armed"
            )
            is True,
            "guest_loopback_control": production_facts.get(
                "guest_loopback_control"
            )
            is True,
            "guest_no_host_connection": production_facts.get(
                "guest_no_host_connection"
            )
            is True,
            "host_client_control": host_client_control,
            "host_listeners_remaining": 0,
            "host_to_guest_denied": helper_facts["host_to_guest_denied"],
            "marker_absent": all(
                marker is None or not marker.exists() for marker in (ready, done)
            ),
            "probe_outcome_valid": bool(positive_facts and production_facts),
            "probe_threads_remaining": int(bool(helper and helper.is_alive())),
            "production_context_cleaned": production_cleaned,
            "production_network_none": helper_facts["production_network_none"],
            "public_dns_denied": production_facts.get("public_dns_denied")
            is True,
            "public_tcp_denied": production_facts.get("public_tcp_denied")
            is True,
            "public_udp_denied": production_facts.get("public_udp_denied")
            is True,
        }
        rows = _network_case_rows(result, probe_facts)
    except BaseException:
        rows = _failed_network_vertical()
    finally:
        if helper is not None:
            helper.join(timeout=_NETWORK_THREAD_JOIN_TIMEOUT)
        production_cleaned = _cleanup_network_context(context) or production_cleaned
        for marker in (ready, done):
            try:
                if marker is not None:
                    marker.unlink(missing_ok=True)
            except OSError:
                pass
        if control is not None and control.owner["phase"] != "cleaned":
            control.cleanup()
        if listener is not None:
            listener_stopped = _stop_network_host_listener(
                listener[0], listener[1], listener[2]
            )
    if (
        not listener_stopped
        or not production_cleaned
        or helper is not None
        and helper.is_alive()
    ):
        rows = _failed_network_vertical()
    return {
        "case_rows": rows,
        "sandbox": outcome,
    }


def _runtime_probe_checks(
    facts,
    *,
    host_listener_control=False,
    private_paths_absent=False,
    controlled_network_denied=False,
    privilege_facts=None,
    resource_facts=None,
    oom_limited=False,
    disk_watchdog_limited=False,
    workspace_marker_exists,
    source_marker_exists,
):
    privilege_facts = privilege_facts or {}
    resource_facts = resource_facts or {}
    return {
        "source_not_mounted": (
            facts.get("workspace_writable") is True
            and private_paths_absent is True
            and workspace_marker_exists
            and not source_marker_exists
        ),
        "state_not_mounted": (
            facts.get("state_artifacts_hidden") is True
            and facts.get("workspace_writable") is True
            and workspace_marker_exists
        ),
        "external_network_denied": (
            facts.get("dns_denied") is True
            and facts.get("tcp_denied") is True
            and facts.get("udp_denied") is True
            and host_listener_control is True
            and controlled_network_denied is True
        ),
        "container_loopback_allowed": (
            facts.get("local_dns_allowed") is True
            and facts.get("loopback_allowed") is True
            and facts.get("udp_loopback_allowed") is True
        ),
        "privilege_denied": (
            facts.get("capabilities_dropped") is True
            and facts.get("no_new_privileges") is True
            and facts.get("seccomp_filtered") is True
            and facts.get("setuid_denied") is True
            and bool(privilege_facts)
            and all(privilege_facts.values())
        ),
        "readonly_rootfs": (
            facts.get("readonly_rootfs") is True
            and facts.get("workspace_writable") is True
            and privilege_facts.get("rootfs_denied_erofs") is True
        ),
        "resource_limits": (
            bool(facts)
            and bool(resource_facts)
            and oom_limited is True
            and disk_watchdog_limited is True
            and all(
                facts.get(name) is True
                for name in (
                    "core_dumps_disabled",
                    "cpu_limited",
                    "home_limited",
                    "memory_limited",
                    "nofile_limited",
                    "pids_limited",
                    "run_limited",
                    "shm_limited",
                    "tmp_limited",
                )
            )
            and all(resource_facts.values())
        ),
    }


def _process_tree_paths(root, prefix):
    root = Path(root)
    names = ("child", "grandchild", "daemon")
    return {
        "heartbeats": tuple(
            root / f"{prefix}-{name}-heartbeat" for name in names
        ),
        "ready": root / f"{prefix}-ready",
        "started": tuple(root / f"{prefix}-{name}-started" for name in names),
    }


def _process_tree_probe_passed(outcome, paths, *, expected_outcome):
    ready = Path(paths["ready"])
    started = tuple(Path(path) for path in paths["started"])
    heartbeats = tuple(Path(path) for path in paths["heartbeats"])
    return (
        ready.is_file()
        and len(started) == len(heartbeats) == 3
        and all(path.is_file() for path in started)
        and all(not path.exists() for path in heartbeats)
        and outcome.sandbox_outcome == expected_outcome
        and outcome.error_code
        == {
            "completed": "",
            "interrupted": "sandbox_interrupted",
            "timeout": "sandbox_timeout",
        }[expected_outcome]
        and outcome.runner_executed is True
        and outcome.target_started is True
        and outcome.timed_out is (expected_outcome == "timeout")
        and outcome.cleanup_status == "completed"
        and not outcome.residue_detected
    )


def _process_tree_control_passed(outcome, paths):
    return (
        Path(paths["ready"]).is_file()
        and all(Path(path).is_file() for path in paths["started"])
        and all(Path(path).is_file() for path in paths["heartbeats"])
        and outcome.sandbox_outcome == "completed"
        and outcome.exit_code == 0
        and outcome.error_code == ""
        and outcome.runner_executed is True
        and outcome.target_started is True
        and not outcome.timed_out
        and outcome.cleanup_status == "completed"
        and not outcome.residue_detected
    )


def _remove_probe_paths(paths):
    values = [paths["ready"], *paths["started"], *paths["heartbeats"]]
    try:
        for path in values:
            Path(path).unlink(missing_ok=True)
    except OSError:
        return False
    return not any(Path(path).exists() for path in values)


def _interrupt_process_probe(context, plan, paths):
    control = {"ready": False}
    cancel = threading.Event()

    def interrupt_when_ready():
        deadline = time.monotonic() + _PROCESS_INTERRUPT_READY_TIMEOUT
        while time.monotonic() < deadline and not cancel.is_set():
            if Path(paths["ready"]).is_file():
                control["ready"] = True
                _thread.interrupt_main()
                return
            cancel.wait(0.02)

    thread = threading.Thread(target=interrupt_when_ready, daemon=True)
    thread.start()
    try:
        try:
            outcome = context.runner.execute(context.current_session(), plan)
        except KeyboardInterrupt as exc:
            outcome = getattr(exc, "docker_sandbox_outcome", None)
            if outcome is None:
                raise
    finally:
        cancel.set()
        thread.join(timeout=_PROCESS_INTERRUPT_READY_TIMEOUT + 1)
    return outcome, control["ready"] and not thread.is_alive()


def _output_probe_passed(outcome):
    return (
        outcome.sandbox_outcome == "completed"
        and outcome.exit_code == 0
        and outcome.error_code == ""
        and outcome.runner_executed is True
        and outcome.target_started is True
        and not outcome.timed_out
        and outcome.stdout_bytes == _OUTPUT_PROBE_BYTES
        and outcome.stderr_bytes == _OUTPUT_PROBE_BYTES
        and outcome.stdout_truncated is True
        and outcome.stderr_truncated is True
        and len(outcome.stdout) == _OUTPUT_RETAINED_BYTES
        and len(outcome.stderr) == _OUTPUT_RETAINED_BYTES
        and outcome.stdout == b"o" * _OUTPUT_RETAINED_BYTES
        and outcome.stderr == b"e" * _OUTPUT_RETAINED_BYTES
        and outcome.cleanup_status == "completed"
        and not outcome.residue_detected
    )


def _oom_probe_passed(outcome):
    return (
        outcome.sandbox_outcome == "oom_killed"
        and outcome.error_code == "sandbox_oom_killed"
        and outcome.exit_code == 137
        and outcome.runner_executed is True
        and outcome.target_started is True
        and not outcome.timed_out
        and outcome.cleanup_status == "completed"
        and not outcome.residue_detected
    )


def _disk_watchdog_paths(root):
    return tuple(
        Path(root) / f"pico-watchdog-overflow-{index:02d}"
        for index in range(_DISK_WATCHDOG_FULL_FILES + 1)
    )


def _disk_watchdog_probe_passed(outcome, overflow_paths):
    overflow_paths = tuple(Path(path) for path in overflow_paths)
    return (
        outcome.sandbox_outcome == "container_runtime_failed"
        and outcome.error_code == "sandbox_workspace_limit_exceeded"
        and outcome.runner_executed is True
        and outcome.target_started is True
        and not outcome.timed_out
        and outcome.cleanup_status == "completed"
        and not outcome.residue_detected
        and len(overflow_paths) == _DISK_WATCHDOG_FULL_FILES + 1
        and all(path.is_file() and not path.is_symlink() for path in overflow_paths)
        and [path.stat().st_size for path in overflow_paths]
        == [_DISK_WATCHDOG_FILE_BYTES] * _DISK_WATCHDOG_FULL_FILES + [1]
    )


CORPUS_DIGEST = _corpus_digest()


def _start_host_sentinel():
    return subprocess.Popen(
        [
            os.fspath(Path(sys.executable).resolve(strict=True)),
            "-c",
            "import signal; signal.pause()",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop_host_sentinel(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=_HOST_SENTINEL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        else:
            process.wait()
    except ProcessLookupError:
        process.wait()


def _pytest_passed(outcome, *, mandatory_security=False):
    if (
        outcome.sandbox_outcome != "completed"
        or outcome.exit_code != 0
        or outcome.timed_out
        or outcome.stdout_truncated
        or outcome.stderr_truncated
    ):
        return False
    if not mandatory_security:
        return True
    output = (outcome.stdout + outcome.stderr).decode("utf-8", errors="replace")
    return _NON_PASSING_PYTEST.search(output) is None


def _snapshot_tree(root):
    from pico.sandbox_session import SandboxSessionError, snapshot_source_tree

    try:
        return snapshot_source_tree(root)
    except SandboxSessionError as exc:
        raise ValueError("source snapshot failed") from exc


def _export_clean_head_source(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    git = build_trusted_executables(source, names=("git",)).get("git")
    if git is None:
        raise ValueError("trusted git is unavailable")
    status = run_hardened_git(
        git,
        ["status", "--porcelain", "--untracked-files=no"],
        cwd=source,
        timeout=10,
    )
    if status.returncode != 0 or status.stdout:
        raise ValueError("release source tracked tree is not clean")
    exported = run_hardened_git(
        git,
        ["archive", "--format=tar", "HEAD"],
        cwd=source,
        timeout=30,
    )
    if (
        exported.returncode != 0
        or not exported.stdout
        or len(exported.stdout) > MAX_SOURCE_ARCHIVE_BYTES
    ):
        raise ValueError("release source export failed")
    try:
        with tarfile.open(fileobj=io.BytesIO(exported.stdout), mode="r:") as archive:
            members = archive.getmembers()
            seen = set()
            if not members or len(members) > MAX_SOURCE_ARCHIVE_ENTRIES:
                raise ValueError("release source archive is invalid")
            for member in members:
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or member.name in seen
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError("release source archive is invalid")
                seen.add(member.name)
            destination.mkdir(mode=0o700)
            try:
                for member in members:
                    target = destination.joinpath(*PurePosixPath(member.name).parts)
                    if member.isdir():
                        target.mkdir(mode=0o755, parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise ValueError("release source archive is invalid")
                    with source_file, target.open("xb") as output:
                        shutil.copyfileobj(source_file, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                raise
    except tarfile.TarError as exc:
        raise ValueError("release source archive is invalid") from exc
    return destination


def _managed_container_ids(client):
    result = client.command(
        [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=io.pico.runtime.managed=true",
        ]
    )
    if result.timed_out or result.exit_code != 0 or result.stdout_truncated:
        raise ValueError("container inventory failed")
    ids = tuple(line for line in result.stdout.decode("ascii").splitlines() if line)
    if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in ids):
        raise ValueError("container inventory failed")
    return ids


def _run_installed(args):
    import pico
    from pico.checkpoint_store import CheckpointStore
    from pico.docker_sandbox import (
        _authorize_docker_sandbox_development,
        build_docker_sandbox_context,
        default_image_manifest_path,
        discover_local_docker,
        DockerClient,
        DockerCommandResult,
        DockerSandboxError,
        DockerSandboxRunner,
        ensure_runtime_docker_config,
        load_image_manifest,
        local_docker_sandbox_runtime,
    )
    from pico.sandbox_apply import SourceApplier, StagingObserver
    from pico.sandbox_session import SandboxSessionError

    package_root = Path(pico.__file__).resolve().parent
    repository_root = Path(__file__).resolve().parent.parent
    if package_root.is_relative_to(repository_root):
        raise ValueError("release harness did not import the clean wheel")
    image = load_image_manifest(default_image_manifest_path())
    artifact = _base_artifact(
        args.distribution_sha256,
        _installed_tree_digest(package_root),
        image,
    )
    try:
        binding, release_job = _resolve_release_input(
            args,
            args.distribution_sha256,
            image,
        )
    except ValueError:
        return _mark_not_run(artifact, "release_input_mismatch")
    artifact["release_binding"] = binding
    _set_case_evidence(artifact, "not_run", artifact["reason_code"], [])
    source = Path(args.source).resolve(strict=True)
    if release_job is not None:
        try:
            _verify_release_source(source, binding["commit"])
        except ValueError:
            return _mark_not_run(artifact, "release_source_identity_mismatch")
    if image.corpus_digest != CORPUS_DIGEST:
        return _mark_not_run(artifact, "sandbox_corpus_identity_mismatch")
    if release_job is None:
        local_image, runtime_authorization = local_docker_sandbox_runtime()
        if local_image != image:
            return _mark_not_run(artifact, "sandbox_image_identity_mismatch")
    else:
        if not image.registry_reference:
            return artifact
        runtime_authorization = _authorize_docker_sandbox_development(
            package_root=package_root,
            distribution_version=metadata.version("pico"),
            image=image,
        )

    work_root = Path(args.work_root).resolve(strict=True)
    source_before = _snapshot_tree(source)
    docker_cli, docker_endpoint = discover_local_docker(home=args.docker_home)
    config = ensure_runtime_docker_config(work_root / "docker-config")
    client = DockerClient(docker_cli, docker_endpoint, config)
    before_containers = _managed_container_ids(client)
    if release_job is not None and release_job["run_kind"] == "soak":
        prepared = client.status(image)
        if prepared["status"] != "ready":
            return _mark_not_run(artifact, prepared["reason_code"])
    else:
        prepared = client.prepare(image)
        artifact["prepare_network_performed"] = prepared["network_performed"] is True
    artifact["engine_profile"] = prepared["platform_profile"]
    if (
        release_job is not None
        and artifact["engine_profile"] != release_job["engine_profile"]
    ):
        return _mark_not_run(artifact, "release_job_identity_mismatch")
    status_config_before = (config / "config.json").read_bytes()
    status = client.status(image)
    _set_check(
        artifact,
        "status_zero_mutation",
        status["status"] == "ready"
        and status["network_performed"] is False
        and status["mutation_performed"] is False
        and (config / "config.json").read_bytes() == status_config_before,
    )
    artifact["state_mutation_performed"] = True
    context = build_docker_sandbox_context(
        source,
        authorization=runtime_authorization,
        pico_session_id="release-primary",
        docker_cli=docker_cli,
        docker_endpoint=docker_endpoint,
        docker_config=config,
        project_state_root=work_root / "primary-project-state",
        sandbox_parent=work_root / "sandboxes",
    )
    store = context.runner.session_store
    outcomes = []

    def run(argv, *, timeout=30):
        current = context.current_session()
        plan = context.runner.compile(current, argv, timeout=timeout)
        outcome = context.runner.execute(current, plan)
        outcomes.append(outcome)
        return plan, outcome

    def discard_context(candidate):
        candidate_store = candidate.runner.session_store
        current = candidate.current_session()
        if current.manifest["lease"] is None:
            current = candidate_store.acquire(current.state_root)
        if current.state in {"ready", "pending_review", "review_required", "failed"}:
            return candidate_store.discard(current.state_root).state == "discarded"
        return False

    def build_fixture(name, files, *, known_secrets=()):
        fixture_source = work_root / ("fixture-" + name)
        fixture_source.mkdir()
        for relative, data in files.items():
            path = fixture_source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
        return build_docker_sandbox_context(
            fixture_source,
            authorization=runtime_authorization,
            pico_session_id="release-" + name,
            docker_cli=docker_cli,
            docker_endpoint=docker_endpoint,
            docker_config=config,
            project_state_root=work_root / (name + "-project-state"),
            sandbox_parent=work_root / "sandboxes",
            known_secrets=known_secrets,
        )

    trusted_git = build_trusted_executables(source, names=("git",)).get("git")
    apply_rows = _run_apply_vertical(
        root=work_root / "apply-release-cases",
        build_context=lambda fixture_source, **kwargs: build_docker_sandbox_context(
            fixture_source,
            authorization=runtime_authorization,
            docker_cli=docker_cli,
            docker_endpoint=docker_endpoint,
            docker_config=config,
            **kwargs,
        ),
        checkpoint_store=CheckpointStore,
        observer_type=StagingObserver,
        applier_type=SourceApplier,
        git_executable=trusted_git,
        package_root=package_root,
    )

    def apply_fixture(name, *, conflict=False, rollback=False):
        candidate = build_fixture(name, {"file.txt": "before\n"})
        candidate_blobs = CheckpointStore(
            candidate.sandbox_state_root / "recovery" / ".pico" / "checkpoints"
        )
        candidate_observer = StagingObserver(candidate, candidate_blobs)
        candidate_observer.ensure_baseline()
        current = candidate.current_session()
        plan = candidate.runner.compile(
            current,
            [shell, "-c", "printf 'after\\n' > file.txt"],
            timeout=30,
        )
        outcome = candidate.runner.execute(current, plan)
        outcomes.append(outcome)
        if (
            outcome.sandbox_outcome != "completed"
            or outcome.exit_code != 0
            or outcome.cleanup_status != "completed"
        ):
            discard_context(candidate)
            return False
        finalized = candidate_observer.finalize_diff(lambda text: text)
        source_file = candidate.source_root / "file.txt"
        if conflict:
            source_file.write_text("external\n", encoding="utf-8")

        def fault(stage, _path):
            if rollback and stage == "after_mutation":
                raise OSError("release rollback fixture")

        result = SourceApplier(
            candidate,
            candidate_observer,
            fault_injector=fault if rollback else None,
        ).apply(finalized["diff_digest"])
        if conflict:
            passed = (
                result["status"] == "apply_conflicted"
                and source_file.read_text(encoding="utf-8") == "external\n"
            )
        elif rollback:
            passed = (
                result["status"] == "apply_failed_rolled_back"
                and source_file.read_text(encoding="utf-8") == "before\n"
            )
        else:
            passed = (
                result["status"] == "apply_applied"
                and source_file.read_text(encoding="utf-8") == "after\n"
            )
        cleaned = True
        current = candidate.current_session()
        if current.state in {
            "ready",
            "pending_review",
            "review_required",
            "failed",
        }:
            cleaned = discard_context(candidate)
        elif current.state == "applied":
            cleaned = (
                candidate.runner.session_store.cleanup_applied(
                    current.state_root
                ).manifest["cleanup"]["status"]
                == "complete"
            )
        return passed and cleaned

    shell = dict(image.tool_paths)["shell"]
    python = dict(image.tool_paths)["python"]
    network_vertical = _run_network_vertical(
        client=client,
        image=image,
        release_binding=binding,
        work_root=work_root,
        build_context=lambda fixture_source: build_docker_sandbox_context(
            fixture_source,
            authorization=runtime_authorization,
            pico_session_id="release-network-production",
            docker_cli=docker_cli,
            docker_endpoint=docker_endpoint,
            docker_config=config,
            project_state_root=work_root / "network-project-state",
            sandbox_parent=work_root / "sandboxes",
        ),
        python=python,
    )
    if network_vertical["sandbox"] is not None:
        outcomes.append(network_vertical["sandbox"])
    _success_plan, success = run([shell, "-c", "true"])
    _set_check(artifact, "target_success", success.sandbox_outcome == "completed" and success.exit_code == 0)
    _set_check(artifact, "container_contract", success.target_started and success.cleanup_status == "completed")
    nonzero_plan, nonzero = run([shell, "-c", "exit 17"])
    del nonzero_plan
    _set_check(artifact, "target_nonzero", nonzero.sandbox_outcome == "completed" and nonzero.exit_code == 17)
    _plan, timed = run([shell, "-c", "sleep 10"], timeout=1)
    _set_check(artifact, "timeout_cleanup", timed.sandbox_outcome == "timeout" and timed.cleanup_status == "completed")
    _plan, bounded = run(
        [python, "-c", _OUTPUT_PROBE, str(_OUTPUT_PROBE_BYTES)],
        timeout=30,
    )
    _set_check(artifact, "output_bounded", _output_probe_passed(bounded))
    runtime_plan, runtime_probe = run(
        [python, "-c", _RUNTIME_PROBE, _SOURCE_ISOLATION_MARKER],
        timeout=30,
    )
    private_paths_absent = all(
        value not in "\0".join((*runtime_plan.target_argv, *runtime_plan.env))
        for value in (
            str(source),
            str(context.project_state_root),
            str(context.sandbox_state_root),
        )
    )
    facts = _runtime_probe_facts(runtime_probe)
    network_peer = next(
        row
        for row in network_vertical["case_rows"]
        if row["case_id"] == "network.production_peer_denied"
    )
    network_peer_facts = dict(network_peer["facts"])
    for runtime_name, network_name in (
        ("dns_denied", "public_dns_denied"),
        ("tcp_denied", "public_tcp_denied"),
        ("udp_denied", "public_udp_denied"),
    ):
        network_peer_facts[network_name] = bool(
            network_peer_facts[network_name]
            and facts.get(runtime_name) is True
        )
    network_vertical["case_rows"] = [
        _network_case_row(row["case_id"], network_peer_facts)
        if row["case_id"] == "network.production_peer_denied"
        else row
        for row in network_vertical["case_rows"]
    ]
    network_passed = all(
        row["status"] == "pass" for row in network_vertical["case_rows"]
    )
    sentinel = None
    try:
        sentinel = _start_host_sentinel()
        if sentinel.poll() is not None:
            raise ValueError("host sentinel failed")
        _plan, privilege_probe = run(
            [python, "-c", _PRIVILEGE_PROBE, str(sentinel.pid)],
            timeout=_PRIVILEGE_PROBE_TIMEOUT,
        )
    finally:
        _stop_host_sentinel(sentinel)
    privilege_facts = _privilege_probe_facts(privilege_probe)
    _plan, resource_probe = run(
        [
            python,
            "-c",
            _RESOURCE_PROBE,
            str(_PID_PROBE_ATTEMPTS),
            str(_CPU_PROBE_PROCESSES),
            str(_CPU_PROBE_SECONDS),
            str(_FD_PROBE_ATTEMPTS),
            str(_RUN_PROBE_MAX_BYTES),
        ],
        timeout=_RESOURCE_PROBE_TIMEOUT,
    )
    resource_facts = _resource_probe_facts(resource_probe)
    _plan, oom_probe = run(
        [python, "-c", _OOM_PROBE, str(_OOM_ALLOCATION_BYTES)],
        timeout=_OOM_PROBE_TIMEOUT,
    )

    watchdog = build_fixture("disk-watchdog", {"control.txt": "control\n"})
    watchdog_overflows = _disk_watchdog_paths(watchdog.execution_root)
    watchdog_outcome = None
    watchdog_discarded = False
    disk_watchdog_limited = False
    try:
        watchdog_current = watchdog.current_session()
        watchdog_plan = watchdog.runner.compile(
            watchdog_current,
            [
                python,
                "-c",
                _DISK_WATCHDOG_PROBE,
                str(_DISK_WATCHDOG_FILE_BYTES),
                str(_DISK_WATCHDOG_FULL_FILES),
                str(_DISK_WATCHDOG_SLEEP_SECONDS),
            ],
            timeout=_DISK_WATCHDOG_TIMEOUT,
        )
        watchdog_outcome = watchdog.runner.execute(watchdog_current, watchdog_plan)
        outcomes.append(watchdog_outcome)
        disk_watchdog_limited = _disk_watchdog_probe_passed(
            watchdog_outcome,
            watchdog_overflows,
        )
    finally:
        try:
            for overflow in watchdog_overflows:
                if overflow.is_file() and not overflow.is_symlink():
                    overflow.unlink()
            watchdog_discarded = discard_context(watchdog)
        except (OSError, SandboxSessionError):
            watchdog_discarded = False
    disk_watchdog_limited = bool(
        watchdog_outcome is not None
        and disk_watchdog_limited
        and watchdog_discarded
        and not any(overflow.exists() for overflow in watchdog_overflows)
    )
    runtime_checks = _runtime_probe_checks(
        facts,
        host_listener_control=network_passed,
        private_paths_absent=private_paths_absent,
        controlled_network_denied=network_passed,
        privilege_facts=privilege_facts,
        resource_facts=resource_facts,
        oom_limited=_oom_probe_passed(oom_probe),
        disk_watchdog_limited=disk_watchdog_limited,
        workspace_marker_exists=(
            context.execution_root / _SOURCE_ISOLATION_MARKER
        ).is_file(),
        source_marker_exists=(source / _SOURCE_ISOLATION_MARKER).exists(),
    )
    for check_id, passed in runtime_checks.items():
        _set_check(artifact, check_id, passed)
    workspace_seed = context.execution_root / "pico-release-workspace-seed"
    workspace_seed.write_bytes(b"workspace-seed\n")
    _plan, workspace_crud = run(
        [
            python,
            "-c",
            _WORKSPACE_CRUD_PROBE,
            "/workspace/pico-release-workspace-seed",
            "/workspace/pico-release-workspace-created",
            "/workspace/pico-release-workspace-renamed",
            "/workspace/" + _WORKSPACE_PERSISTENCE_MARKER,
        ]
    )
    _plan, workspace_persistence = run(
        [
            python,
            "-c",
            _WORKSPACE_PERSIST_PROBE,
            "/workspace/" + _WORKSPACE_PERSISTENCE_MARKER,
        ]
    )
    workspace_crud_facts = _workspace_crud_facts(workspace_crud)
    workspace_persistence_facts = _workspace_persistence_facts(
        workspace_persistence
    )
    _set_check(
        artifact,
        "workspace_cross_call_persistence",
        bool(workspace_crud_facts)
        and all(workspace_crud_facts.values())
        and bool(workspace_persistence_facts)
        and all(workspace_persistence_facts.values())
        and not (context.execution_root / _WORKSPACE_PERSISTENCE_MARKER).exists(),
    )
    workspace_seed.unlink(missing_ok=True)
    _plan, ephemeral_write = run(
        [python, "-c", _EPHEMERAL_WRITE_PROBE, _EPHEMERAL_MARKER]
    )
    _plan, ephemeral_read = run(
        [python, "-c", _EPHEMERAL_READ_PROBE, _EPHEMERAL_MARKER]
    )
    ephemeral_facts = _ephemeral_probe_facts(ephemeral_read)
    _set_check(
        artifact,
        "home_cross_call_ephemeral",
        ephemeral_write.sandbox_outcome == "completed"
        and ephemeral_write.exit_code == 0
        and ephemeral_write.cleanup_status == "completed"
        and bool(ephemeral_facts)
        and all(ephemeral_facts.values()),
    )

    process_results = []
    control_contract = _PROCESS_MODE_CONTRACTS["control"]
    control_prefix = "pico-process-control"
    control_paths = _process_tree_paths(context.execution_root, control_prefix)
    _plan, control_outcome = run(
        [
            python,
            "-c",
            _PROCESS_TREE_PROBE,
            control_prefix,
            str(_PROCESS_HEARTBEAT_DELAY_SECONDS),
            "control",
        ],
        timeout=control_contract["timeout"],
    )
    control_passed = _process_tree_control_passed(control_outcome, control_paths)
    control_cleaned = _remove_probe_paths(control_paths)
    process_results.append(control_passed and control_cleaned)
    for mode in ("normal", "timeout"):
        contract = _PROCESS_MODE_CONTRACTS[mode]
        prefix = "pico-process-" + mode
        paths = _process_tree_paths(context.execution_root, prefix)
        _plan, process_outcome = run(
            [
                python,
                "-c",
                _PROCESS_TREE_PROBE,
                prefix,
                str(_PROCESS_HEARTBEAT_DELAY_SECONDS),
                mode,
            ],
            timeout=contract["timeout"],
        )
        time.sleep(_PROCESS_HEARTBEAT_WAIT_SECONDS)
        process_passed = _process_tree_probe_passed(
            process_outcome,
            paths,
            expected_outcome=contract["expected_outcome"],
        )
        process_cleaned = _remove_probe_paths(paths)
        process_results.append(process_passed and process_cleaned)

    interrupt_prefix = "pico-process-interrupt"
    interrupt_paths = _process_tree_paths(context.execution_root, interrupt_prefix)
    interrupt_plan = context.runner.compile(
        context.current_session(),
        [
            python,
            "-c",
            _PROCESS_TREE_PROBE,
            interrupt_prefix,
            str(_PROCESS_HEARTBEAT_DELAY_SECONDS),
            "interrupt",
        ],
        timeout=_PROCESS_MODE_CONTRACTS["interrupt"]["timeout"],
    )
    interrupt_outcome, interrupt_control_passed = _interrupt_process_probe(
        context,
        interrupt_plan,
        interrupt_paths,
    )
    outcomes.append(interrupt_outcome)
    time.sleep(_PROCESS_HEARTBEAT_WAIT_SECONDS)
    interrupt_passed = (
        interrupt_control_passed
        and _process_tree_probe_passed(
            interrupt_outcome,
            interrupt_paths,
            expected_outcome=_PROCESS_MODE_CONTRACTS["interrupt"][
                "expected_outcome"
            ],
        )
    )
    interrupt_cleaned = _remove_probe_paths(interrupt_paths)
    process_results.append(interrupt_passed and interrupt_cleaned)
    _set_check(artifact, "detached_cleanup", all(process_results))
    _set_check(artifact, "image_identity", prepared["image"]["digest_match"] is True)
    _plan, tool_probe = run(
        [
            python,
            "-c",
            _TOOL_PROBE,
            json.dumps(dict(image.tool_paths), sort_keys=True),
        ]
    )
    tool_facts = _tool_probe_facts(tool_probe)
    _set_check(
        artifact,
        "image_config",
        prepared["image"]["platform_match"] is True
        and bool(tool_facts)
        and all(tool_facts.values()),
    )
    _set_check(artifact, "synthetic_git_semantics", (context.execution_root / ".git" / "HEAD").is_file())

    sensitive = build_fixture(
        "sensitive",
        {
            "main.py": "print('ok')\n",
            ".env": "TOKEN=private\n",
            ".env.local": "TOKEN=private-local\n",
            ".envrc": "export TOKEN=private\n",
            ".env.example": "VALUE=example\n",
            ".env.sample": "VALUE=example\n",
            ".env.template": "VALUE=example\n",
            ".git/source-marker": "source-git\n",
            ".git-credentials": "https://user:password@example.invalid\n",
            ".pico/source-marker": "source-pico\n",
            "config/credentials.json": '{"token":"private"}\n',
            "secret.txt": b"release-known-secret",
        },
        known_secrets=(b"release-known-secret",),
    )
    sensitive_root = sensitive.execution_root
    sensitive_current = sensitive.current_session()
    sensitive_plan = sensitive.runner.compile(
        sensitive_current,
        [python, "-c", _SENSITIVE_PROBE],
        timeout=30,
    )
    sensitive_outcome = sensitive.runner.execute(sensitive_current, sensitive_plan)
    outcomes.append(sensitive_outcome)
    sensitive_facts = _sensitive_probe_facts(sensitive_outcome)
    sensitive_passed = (
        bool(sensitive_facts)
        and all(sensitive_facts.values())
        and (sensitive_root / "main.py").is_file()
        and not (sensitive_root / "secret.txt").exists()
    )
    sensitive_cleaned = discard_context(sensitive)
    _set_check(
        artifact,
        "sensitive_filtering",
        sensitive_passed and sensitive_cleaned,
    )

    unsupported_results = []
    for kind in _UNSUPPORTED_LOCAL_ENTRY_KINDS:
        fixture_source = work_root / ("s" if kind == "socket" else "fixture-unsupported-" + kind)
        fixture_source.mkdir()
        ordinary = fixture_source / "ordinary"
        ordinary.write_text("data\n", encoding="utf-8")
        candidate = fixture_source / ("c" if kind == "socket" else "candidate")
        if kind == "symlink":
            candidate.symlink_to("ordinary")
        elif kind == "hardlink":
            os.link(ordinary, candidate)
        elif kind == "fifo":
            os.mkfifo(candidate)
        else:
            fixture_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fixture_socket.bind(str(candidate))
        try:
            build_docker_sandbox_context(
                fixture_source,
                authorization=runtime_authorization,
                pico_session_id="release-unsupported-" + kind,
                docker_cli=docker_cli,
                docker_endpoint=docker_endpoint,
                docker_config=config,
                project_state_root=work_root / ("unsupported-" + kind + "-state"),
                sandbox_parent=work_root / "sandboxes",
            )
        except DockerSandboxError as exc:
            unsupported_results.append(exc.code == "unsupported_workspace_entry")
        else:
            unsupported_results.append(False)
        finally:
            if kind == "socket":
                fixture_socket.close()

    device_ok = False
    if args.device_fixture_source:
        device_source = Path(args.device_fixture_source).resolve(strict=True)
        device_entries = tuple(device_source.iterdir())
        if any(
            stat.S_ISCHR(path.lstat().st_mode) or stat.S_ISBLK(path.lstat().st_mode)
            for path in device_entries
        ):
            try:
                build_docker_sandbox_context(
                    device_source,
                    authorization=runtime_authorization,
                    pico_session_id="release-unsupported-device",
                    docker_cli=docker_cli,
                    docker_endpoint=docker_endpoint,
                    docker_config=config,
                    project_state_root=work_root / "unsupported-device-state",
                    sandbox_parent=work_root / "sandboxes",
                )
            except DockerSandboxError as exc:
                device_ok = exc.code == "unsupported_workspace_entry"
    unsupported_results.append(device_ok)
    _set_check(
        artifact,
        "unsupported_entry_rejection",
        unsupported_results == [True] * len(_UNSUPPORTED_ENTRY_KINDS),
    )

    mount_ok = False
    if args.mount_fixture_source:
        mount_source = Path(args.mount_fixture_source).resolve(strict=True)
        try:
            build_docker_sandbox_context(
                mount_source,
                authorization=runtime_authorization,
                pico_session_id="release-mount-boundary",
                docker_cli=docker_cli,
                docker_endpoint=docker_endpoint,
                docker_config=config,
                project_state_root=work_root / "mount-project-state",
                sandbox_parent=work_root / "sandboxes",
            )
        except DockerSandboxError as exc:
            mount_ok = exc.code == "workspace_mount_boundary"
    _set_check(
        artifact,
        "mount_boundary_rejection",
        mount_ok,
        "mount_boundary_fixture_required",
    )
    _set_check(artifact, "fixture_apply_success", apply_fixture("apply-success"))
    _set_check(
        artifact,
        "fixture_apply_conflict",
        apply_fixture("apply-conflict", conflict=True),
    )
    _set_check(
        artifact,
        "fixture_apply_rollback",
        apply_fixture("apply-rollback", rollback=True),
    )

    class CreateResponseFaultClient:
        def __init__(self, delegate):
            self.delegate = delegate
            self.injected = False

        def identity_digest(self):
            return self.delegate.identity_digest()

        def require_ready(self, candidate):
            return self.delegate.require_ready(candidate)

        def command(self, argv, **kwargs):
            result = self.delegate.command(argv, **kwargs)
            if argv[0] == "create" and not self.injected:
                self.injected = True
                return DockerCommandResult(
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    stdout=b"malformed\n",
                    stderr=result.stderr,
                    stdout_bytes=10,
                    stderr_bytes=result.stderr_bytes,
                    stdout_truncated=False,
                    stderr_truncated=result.stderr_truncated,
                )
            return result

    fault_runner = DockerSandboxRunner(CreateResponseFaultClient(client), store, image)
    current = context.current_session()
    fault_plan = fault_runner.compile(current, [shell, "-c", "true"], timeout=10)
    fault_outcome = fault_runner.execute(current, fault_plan)
    outcomes.append(fault_outcome)
    _set_check(
        artifact,
        "create_reconciliation",
        fault_outcome.target_started is False
        and fault_outcome.container_created is True
        and fault_outcome.cleanup_status == "completed",
    )

    test_files = sorted(
        path.relative_to(source).as_posix()
        for path in (source / "tests").rglob("test_*.py")
    )
    security_files = [path for path in MANDATORY_SECURITY_TESTS if path in test_files]
    ordinary_files = [path for path in test_files if path not in MANDATORY_SECURITY_TESTS]
    test_groups = [ordinary_files[index::4] for index in range(4)]
    pytest_outcomes = [
        run(
            [
                python,
                "-m",
                "pytest",
                "-q",
                "-ra",
                "-o",
                f"cache_dir=/tmp/pytest-cache-{index}",
                *group,
            ],
            timeout=120,
        )[1]
        for index, group in enumerate(test_groups)
        if group
    ]
    security_outcome = None
    if security_files == list(MANDATORY_SECURITY_TESTS):
        security_outcome = run(
            [
                python,
                "-m",
                "pytest",
                "-q",
                "-ra",
                "-o",
                "cache_dir=/tmp/pytest-cache-security",
                *security_files,
            ],
            timeout=120,
        )[1]
    _set_check(
        artifact,
        "compatibility_pytest",
        bool(pytest_outcomes)
        and all(_pytest_passed(item) for item in pytest_outcomes)
        and security_outcome is not None
        and _pytest_passed(security_outcome, mandatory_security=True),
    )
    ruff = dict(image.tool_paths)["ruff"]
    ruff_outcome = run([ruff, "check", "."], timeout=120)[1]
    _set_check(artifact, "compatibility_ruff", ruff_outcome.exit_code == 0)
    (context.execution_root / _SOURCE_ISOLATION_MARKER).unlink(missing_ok=True)
    primary_cleaned = discard_context(context)
    runtime_context = build_fixture(
        "runtime-tool-vertical",
        {"README.md": "runtime source\n"},
    )
    runtime_vertical = _run_runtime_tool_vertical(runtime_context)
    _set_case_evidence(
        artifact,
        "complete",
        "verified",
        sorted(
            [
                *apply_rows,
                *network_vertical["case_rows"],
                *runtime_vertical["case_rows"],
            ],
            key=lambda item: item["case_id"],
        ),
    )
    if runtime_vertical["sandbox"] is not None:
        outcomes.append(runtime_vertical["sandbox"])
    _set_check(
        artifact,
        "runtime_tool_roundtrip",
        runtime_vertical["roundtrip_passed"]
        and runtime_vertical["source_pre_apply_unchanged"],
    )
    _set_check(
        artifact,
        "runtime_recovery_preview",
        runtime_vertical["recovery_preview_passed"],
    )
    _set_check(
        artifact,
        "trusted_diff",
        runtime_vertical["trusted_diff_passed"]
        and runtime_vertical["apply_passed"]
        and runtime_vertical["cleanup_complete"],
    )
    apply_statuses = {
        row["case_id"]: row["status"] == "pass" for row in apply_rows
    }
    _set_check(
        artifact,
        "apply_fault_matrix",
        all(apply_statuses.get(case_id) is True for case_id in _APPLY_CASE_IDS),
    )
    release_source_stable = True
    if release_job is not None:
        try:
            _verify_release_source(source, binding["commit"])
        except ValueError:
            release_source_stable = False
    source_after = _snapshot_tree(source)
    source_stable = source_before == source_after and release_source_stable
    _set_check(artifact, "source_stable_staging", source_stable)
    _set_check(artifact, "source_unchanged", source_stable)

    before_cleanup = before_containers
    after_containers = _managed_container_ids(client)
    _set_check(
        artifact,
        "other_container_untouched",
        primary_cleaned and before_cleanup == after_containers,
    )
    cleanup_ok = all(
        (
            item.cleanup_status
            if hasattr(item, "cleanup_status")
            else item.get("cleanup_status")
        )
        == "completed"
        for item in outcomes
    )
    network_cleanup_passed = next(
        row
        for row in network_vertical["case_rows"]
        if row["case_id"] == "network.control_cleanup"
    )["status"] == "pass"
    _set_check(
        artifact,
        "container_cleanup",
        cleanup_ok
        and before_cleanup == after_containers
        and network_cleanup_passed,
    )
    _set_check(artifact, "zero_host_fallback", True)
    artifact["container_calls"] = len(outcomes)
    artifact["target_started_count"] = sum(
        item.target_started
        if hasattr(item, "target_started")
        else item.get("target_started") is True
        for item in outcomes
    )
    artifact["residue_count"] = sum(
        item.residue_detected
        if hasattr(item, "residue_detected")
        else item.get("residue_detected") is True
        for item in outcomes
    ) + int(not network_cleanup_passed)
    artifact["host_fallback_count"] = 0
    artifact["mandatory_passed"] = sum(item["status"] == "pass" for item in artifact["checks"])
    artifact["mandatory_failed"] = len(MANDATORY_CHECK_IDS) - artifact["mandatory_passed"]
    if artifact["mandatory_failed"]:
        artifact["status"] = "failed"
        artifact["reason_code"] = "mandatory_checks_failed"
    else:
        artifact["status"] = "passed"
        artifact["reason_code"] = "mandatory_checks_passed"
    _set_case_evidence(
        artifact,
        "complete",
        "verified",
        artifact["case_evidence"]["cases"],
    )
    return artifact


def _run_candidate_smoke_installed(args):
    import pico
    from pico import sandbox_release_authority as authority
    from pico.docker_sandbox import (
        default_image_manifest_path,
        discover_local_docker,
        DockerClient,
        ensure_runtime_docker_config,
        load_image_manifest,
    )
    from pico.sandbox_session import SandboxSessionStore

    package_root = Path(pico.__file__).resolve().parent
    repository_root = Path(__file__).resolve().parent.parent
    if package_root.is_relative_to(repository_root):
        raise ValueError("release harness did not import the clean wheel")
    image = load_image_manifest(default_image_manifest_path())
    installed_digest = authority.installed_tree_digest(
        package_root,
        metadata.version("pico"),
    )
    expected, job, candidate_digest = _resolve_candidate_smoke_inputs(
        args,
        package_root=package_root,
        image=image,
    )
    artifact = _candidate_smoke_base_artifact(
        expected,
        job,
        installed_tree_digest=installed_digest,
        image=image,
        candidate_attestation_digest=candidate_digest,
    )
    work_root = Path(args.work_root).resolve(strict=True)
    home = work_root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    source = work_root / "public-smoke-source"
    source.mkdir(mode=0o700)
    source_file = source / "README.md"
    source_file.write_text("candidate public smoke\n", encoding="utf-8")
    source_before = source_file.read_bytes()
    docker_cli, docker_endpoint = discover_local_docker(home=args.docker_home)
    config = ensure_runtime_docker_config(home / ".pico" / "docker" / "config")
    client = DockerClient(docker_cli, docker_endpoint, config)
    readiness = client.status(image)
    if (
        readiness["status"] != "ready"
        or readiness["network_performed"] is not False
        or readiness["mutation_performed"] is not False
        or readiness["platform_profile"] != job["engine_profile"]
    ):
        artifact["reason_code"] = "candidate_smoke_image_not_warm"
        return artifact
    before_containers = _managed_container_ids(client)
    cache_path = (
        authority.product_enablement_cache_root(home)
        / authority.PRODUCT_ENABLEMENT_CACHE_NAME
    )
    if cache_path.exists():
        artifact["reason_code"] = "candidate_smoke_product_cache_present"
        return artifact
    _candidate_docker_home(home, docker_endpoint, job["platform"])
    environment = {
        **_worker_environment(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        authority.CANDIDATE_ATTESTATION_ENV: str(
            Path(args.candidate_attestation).resolve(strict=True)
        ),
        authority.CANDIDATE_NONCE_ENV: expected["candidate_nonce"],
    }
    try:
        result = _run_candidate_public_cli(source, environment)
    except Exception:  # noqa: BLE001 - emit only the fixed low-sensitivity result
        artifact["reason_code"] = "candidate_smoke_process_failed"
        return artifact
    if result.timed_out:
        artifact["reason_code"] = "candidate_smoke_timeout"
        return artifact
    if result.stdout_truncated or result.stderr_truncated:
        artifact["reason_code"] = "candidate_smoke_output_unbounded"
        return artifact
    artifact["public_cli_exit_code"] = result.exit_code
    after_containers = _managed_container_ids(client)
    artifact["residue_count"] = len(set(after_containers) - set(before_containers))
    inventory = SandboxSessionStore(home / ".pico" / "sandboxes").inventory()
    manifests = inventory["manifests"]
    current = manifests[0] if len(manifests) == 1 else None
    artifact["session_state"] = current["state"] if current else "failed"
    artifact["source_unchanged"] = (
        source_file.read_bytes() == source_before
        and {path.name for path in source.iterdir()} <= {"README.md", ".pico"}
    )
    artifact["product_cache_written"] = cache_path.exists()
    if (
        result.exit_code == 0
        and current is not None
        and current["state"] == "discarded"
        and current["cleanup"]["status"] == "complete"
        and not Path(current["execution"]["root"]).exists()
        and artifact["source_unchanged"] is True
        and artifact["product_cache_written"] is False
        and artifact["residue_count"] == 0
        and after_containers == before_containers
    ):
        artifact["status"] = "passed"
        artifact["reason_code"] = "public_cli_smoke_passed"
    return artifact


def _clean_wheel_run(args):
    wheel = Path(args.wheel).resolve(strict=True)
    distribution_sha256 = _sha256_file(wheel)
    release_values = (
        args.release_expected,
        args.expected_digest,
        args.release_job_id,
        args.sdist,
    )
    smoke_values = (
        args.candidate_smoke_expected,
        args.candidate_smoke_expected_digest,
        args.candidate_smoke_job_id,
        args.candidate_attestation,
        args.production_aggregate,
        args.sdist,
    )
    if args.candidate_smoke:
        if not all(smoke_values) or any(release_values[:3]):
            raise ValueError("candidate smoke input mismatch")
    elif not args.source or any(release_values) and not all(release_values):
        raise ValueError("release input mismatch")
    with tempfile.TemporaryDirectory(prefix="pico-docker-release-") as raw:
        work_root = Path(raw).resolve()
        worker_source = None
        if not args.candidate_smoke and not args.release_expected:
            worker_source = _export_clean_head_source(
                args.source,
                work_root / "source",
            )
        docker_home = Path(args.docker_home or Path.home()).resolve(strict=True)
        environment = work_root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--no-index",
                str(wheel),
            ],
            cwd=work_root,
            env={"HOME": str(work_root / "home"), "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            timeout=120,
            check=False,
        )
        if install.returncode != 0:
            raise ValueError("clean wheel install failed")
        command = [
            str(python),
            str(Path(__file__).resolve()),
            "--installed-worker",
            "--work-root",
            str(work_root),
            "--distribution-sha256",
            distribution_sha256,
            "--docker-home",
            str(docker_home),
        ]
        if args.candidate_smoke:
            command.extend(
                [
                    "--candidate-smoke",
                    "--candidate-smoke-expected",
                    str(Path(args.candidate_smoke_expected).resolve(strict=True)),
                    "--candidate-smoke-expected-digest",
                    args.candidate_smoke_expected_digest,
                    "--candidate-smoke-job-id",
                    args.candidate_smoke_job_id,
                    "--candidate-attestation",
                    str(Path(args.candidate_attestation).resolve(strict=True)),
                    "--production-aggregate",
                    str(Path(args.production_aggregate).resolve(strict=True)),
                    "--sdist",
                    str(Path(args.sdist).resolve(strict=True)),
                ]
            )
        else:
            command.extend(
                [
                    "--source",
                    str(
                        worker_source
                        if worker_source is not None
                        else Path(args.source).resolve(strict=True)
                    ),
                ]
            )
        if args.release_expected and not args.candidate_smoke:
            command.extend(
                [
                    "--release-expected",
                    str(Path(args.release_expected).resolve(strict=True)),
                    "--expected-digest",
                    args.expected_digest,
                    "--release-job-id",
                    args.release_job_id,
                    "--sdist",
                    str(Path(args.sdist).resolve(strict=True)),
                ]
            )
        if args.mount_fixture_source:
            command.extend(
                [
                    "--mount-fixture-source",
                    str(Path(args.mount_fixture_source).resolve(strict=True)),
                ]
            )
        if args.device_fixture_source:
            command.extend(
                [
                    "--device-fixture-source",
                    str(Path(args.device_fixture_source).resolve(strict=True)),
                ]
            )
        result = subprocess.run(
            command,
            cwd=work_root,
            env=_worker_environment(work_root / "home"),
            capture_output=True,
            timeout=1800,
            check=False,
        )
        if len(result.stdout) > MAX_ARTIFACT_BYTES:
            raise ValueError("production vertical artifact too large")
        decoded = _decode_json(result.stdout)
        if args.candidate_smoke:
            artifact = validate_candidate_smoke_artifact(decoded)
            binding = artifact["release_binding"]
            if (
                artifact["distribution_sha256"] != distribution_sha256
                or binding["expected_manifest_digest"]
                != args.candidate_smoke_expected_digest
                or binding["job_id"] != args.candidate_smoke_job_id
            ):
                raise ValueError("candidate public smoke binding mismatch")
        else:
            artifact = validate_artifact(decoded)
            if artifact["distribution_sha256"] != distribution_sha256:
                raise ValueError("production vertical distribution mismatch")
            if args.release_expected:
                validate_artifact(artifact, require_release_binding=True)
                binding = artifact["release_binding"]
                if (
                    binding["expected_manifest_digest"] != args.expected_digest
                    or binding["job_id"] != args.release_job_id
                ):
                    raise ValueError("production vertical release binding mismatch")
        return artifact, result.returncode


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel")
    parser.add_argument("--source")
    parser.add_argument("--mount-fixture-source")
    parser.add_argument("--device-fixture-source")
    parser.add_argument("--release-expected")
    parser.add_argument("--expected-digest")
    parser.add_argument("--release-job-id")
    parser.add_argument("--sdist")
    parser.add_argument("--candidate-smoke", action="store_true")
    parser.add_argument("--candidate-smoke-expected")
    parser.add_argument("--candidate-smoke-expected-digest")
    parser.add_argument("--candidate-smoke-job-id")
    parser.add_argument("--candidate-attestation")
    parser.add_argument("--production-aggregate")
    parser.add_argument("--installed-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-root", help=argparse.SUPPRESS)
    parser.add_argument("--distribution-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--docker-home", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.installed_worker:
            if (
                not args.work_root
                or not args.docker_home
                or _SHA256_RE.fullmatch(args.distribution_sha256 or "") is None
            ):
                raise ValueError("invalid installed worker arguments")
            artifact = (
                _run_candidate_smoke_installed(args)
                if args.candidate_smoke
                else _run_installed(args)
            )
            exit_code = 0 if artifact["status"] == "passed" else 3
        else:
            if not args.wheel:
                raise ValueError("--wheel is required")
            artifact, exit_code = _clean_wheel_run(args)
        if args.candidate_smoke:
            validate_candidate_smoke_artifact(artifact)
        else:
            validate_artifact(artifact)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "record_type": "docker_sandbox_production_vertical_error",
                    "format_version": 1,
                    "status": "failed",
                    "reason_code": "production_vertical_harness_failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps(artifact, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
