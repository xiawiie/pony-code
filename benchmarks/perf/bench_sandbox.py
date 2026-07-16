"""Report-only benchmarks for Docker Sandbox production owners."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import shutil
from types import SimpleNamespace
import statistics
import subprocess
import tempfile
import time

from benchmarks.perf.harness import bench
from pico.state.checkpoint_store import CheckpointStore
from pico.sandbox.docker import (
    build_docker_sandbox_context,
    default_image_manifest_path,
    discover_local_docker,
    ensure_runtime_docker_config,
    load_image_manifest,
    local_docker_sandbox_runtime,
    MOUNT_POLICY_DIGEST,
    RESOURCE_POLICY_DIGEST,
)
from pico.sandbox.apply import StagingObserver
from pico.sandbox.session import (
    MAX_FILE_BYTES,
    SandboxSessionStore,
    stage_source,
)


SCENARIO_NAMES = (
    "sandbox/image_manifest/warm",
    "sandbox/session_inspect/warm",
    "sandbox/inventory_parallel/1",
    "sandbox/inventory_parallel/4",
    "sandbox/inventory_parallel/16",
    "sandbox/staging/5000x1k",
    "sandbox/staging/128mib",
    "sandbox/capture_call/noop_326",
    "sandbox/shell_empty_observed/326",
    "sandbox/shell_empty_before_capture/326",
    "sandbox/shell_empty_container/326",
    "sandbox/shell_empty_after_capture/326",
)
ROOT = Path(__file__).resolve().parents[2]
REPORT_IMAGE_PLATFORM = "linux/arm64"


def _report_image():
    return load_image_manifest(
        default_image_manifest_path(),
        target_platform=REPORT_IMAGE_PLATFORM,
    )


def _git_value(args):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip()


def build_artifact(scenarios, *, docker=None):
    image = _report_image()
    dirty_value = _git_value(["status", "--porcelain"])
    return {
        "record_type": "sandbox_perf",
        "format_version": 1,
        "suite": "sandbox-performance-report-only",
        "status": "measured",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "benchmarks/perf/bench_sandbox.py",
        "runtime": {
            "commit": _git_value(["rev-parse", "HEAD"]),
            "dirty": "unknown" if dirty_value == "unknown" else bool(dirty_value),
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "machine": platform.machine().lower(),
            "kernel": platform.release(),
            "docker": docker or {"status": "not_measured"},
        },
        "sandbox": {
            "implementation": "docker_container",
            "image_digest": image.image_digest,
            "policy_digest": image.policy_digest,
            "network_mode": "none",
        },
        "baseline": {
            "status": "not_established",
            "comparison": "report_only",
        },
        "scenarios": [{**scenario, "status": "measured"} for scenario in scenarios],
    }


def _bootstrap(request):
    git = request.workspace_view.physical_root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(
        "ref: refs/heads/pico-sandbox\n",
        encoding="utf-8",
    )
    return "a" * 40


def _session_metadata(image):
    return {
        "engine": {
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "benchmark",
            "server_version": "benchmark",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        "image": {
            "image_digest": image.image_digest,
            "image_id": image.image_id,
            "platform": image.platform,
        },
        "policy": {
            "version": 1,
            "digest": image.policy_digest,
            "network": "none",
            "mount_digest": MOUNT_POLICY_DIGEST,
            "resource_digest": RESOURCE_POLICY_DIGEST,
        },
    }


def _fixture(root):
    image = _report_image()
    source = root / "source"
    source.mkdir()
    (source / "README.md").write_text("benchmark\n", encoding="utf-8")
    store = SandboxSessionStore(root / "sandboxes")
    session = store.create(
        source,
        pico_session_id="benchmark-session",
        bootstrap_git=_bootstrap,
        **_session_metadata(image),
    )
    return store, session


def _staging_benchmarks(root):
    root.mkdir()
    small_source = root / "source-5000"
    small_source.mkdir()
    payload = b"x" * 1024
    for index in range(5000):
        (small_source / f"file-{index:05d}.bin").write_bytes(payload)

    large_source = root / "source-128mib"
    large_source.mkdir()
    with (large_source / "large.bin").open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES)

    counter = 0

    def stage(source, label):
        nonlocal counter
        counter += 1
        destination = root / f"destination-{label}-{counter}"
        stage_source(source, destination)
        return destination

    return [
        bench(
            SCENARIO_NAMES[5],
            lambda: stage(small_source, "small"),
            iterations=5,
            warmup=1,
            cleanup=lambda path: shutil.rmtree(path),
        ),
        bench(
            SCENARIO_NAMES[6],
            lambda: stage(large_source, "large"),
            iterations=5,
            warmup=1,
            cleanup=lambda path: shutil.rmtree(path),
        ),
    ]


def _populate_representative_source(root):
    root.mkdir()
    payload = b"x" * (16 * 1024)
    for index in range(326):
        directory = root / f"group-{index // 50:02d}"
        directory.mkdir(exist_ok=True)
        (directory / f"file-{index:03d}.bin").write_bytes(payload)


class _ObserverContext:
    def __init__(self, source, store, session):
        self.source_root = source
        self.execution_root = session.workspace_view.physical_root
        self.project_state_root = source / ".pico"
        self.sandbox_state_root = session.state_root
        self.source_apply_state_root = session.state_root
        self.sandbox_session = session
        self.runner = SimpleNamespace(session_store=store)

    def current_session(self):
        return self.runner.session_store.inspect(self.sandbox_state_root)


def _capture_benchmark(root):
    root.mkdir()
    image = _report_image()
    source = root / "source"
    _populate_representative_source(source)
    store = SandboxSessionStore(root / "sandboxes")
    session = store.create(
        source,
        pico_session_id="capture-benchmark",
        bootstrap_git=_bootstrap,
        **_session_metadata(image),
    )
    context = _ObserverContext(source, store, session)
    blobs = CheckpointStore(
        session.state_root / "recovery" / ".pico" / "checkpoints"
    )
    observer = StagingObserver(context, blobs)
    observer.ensure_baseline()
    return bench(
        SCENARIO_NAMES[7],
        observer.capture_call_start,
        iterations=20,
        warmup=5,
    )


def _percentile(samples, percentile):
    ordered = sorted(samples)
    index = (len(ordered) - 1) * percentile / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return int(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower))


def _scenario_from_samples(name, samples):
    return {
        "name": name,
        "iterations": len(samples),
        "median_ns": int(statistics.median(samples)),
        "p95_ns": _percentile(samples, 95),
        "min_ns": min(samples),
    }


def _real_shell_benchmarks(root):
    root.mkdir()
    source = root / "source"
    _populate_representative_source(source)
    image, authorization = local_docker_sandbox_runtime()
    docker_cli, docker_endpoint = discover_local_docker()
    context = build_docker_sandbox_context(
        source,
        authorization=authorization,
        pico_session_id="real-benchmark",
        docker_cli=docker_cli,
        docker_endpoint=docker_endpoint,
        docker_config=ensure_runtime_docker_config(root / "docker-config"),
        project_state_root=root / "project-state",
        sandbox_parent=root / "sandboxes",
        image=image,
    )
    blobs = CheckpointStore(
        context.sandbox_state_root / "recovery" / ".pico" / "checkpoints"
    )
    observer = StagingObserver(context, blobs)
    observer.ensure_baseline()
    samples = {"total": [], "before": [], "container": [], "after": []}

    try:
        for index in range(25):
            current = context.current_session()
            plan = context.runner.compile(
                current,
                ["/bin/sh", "-c", ":"],
                timeout=30,
            )
            started = time.perf_counter_ns()
            before = observer.capture_call_start()
            before_done = time.perf_counter_ns()
            outcome = context.runner.execute(current, plan)
            container_done = time.perf_counter_ns()
            after = observer.capture_call_end()
            finished = time.perf_counter_ns()
            if (
                outcome.sandbox_outcome != "completed"
                or observer.diff(before, after)["changed_paths"]
            ):
                raise RuntimeError("real sandbox no-op benchmark failed")
            if index >= 5:
                samples["total"].append(finished - started)
                samples["before"].append(before_done - started)
                samples["container"].append(container_done - before_done)
                samples["after"].append(finished - container_done)
        engine = dict(context.current_session().manifest["engine"])
    finally:
        current = context.current_session()
        if current.state == "ready":
            context.runner.session_store.discard(current.state_root)

    return (
        [
            _scenario_from_samples(SCENARIO_NAMES[8], samples["total"]),
            _scenario_from_samples(SCENARIO_NAMES[9], samples["before"]),
            _scenario_from_samples(SCENARIO_NAMES[10], samples["container"]),
            _scenario_from_samples(SCENARIO_NAMES[11], samples["after"]),
        ],
        {
            "status": "measured",
            "client_version": engine["client_version"],
            "server_version": engine["server_version"],
            "api_version": engine["api_version"],
            "profile": engine["profile"],
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        owner_root = root / "owner"
        owner_root.mkdir()
        store, session = _fixture(owner_root)
        scenarios = [
            bench(
                SCENARIO_NAMES[0],
                _report_image,
                iterations=50,
            ),
            bench(
                SCENARIO_NAMES[1],
                lambda: store.inspect(session.state_root),
                iterations=50,
            ),
        ]
        for index, workers in enumerate((1, 4, 16), start=2):
            with ThreadPoolExecutor(max_workers=workers) as executor:
                scenarios.append(
                    bench(
                        SCENARIO_NAMES[index],
                        lambda workers=workers: list(
                            executor.map(lambda _item: store.inventory(), range(workers))
                        ),
                        iterations=20,
                    )
                )
        scenarios.extend(_staging_benchmarks(root / "staging"))
        scenarios.append(_capture_benchmark(root / "capture"))
        docker = None
        if args.real:
            real_scenarios, docker = _real_shell_benchmarks(root / "real")
            scenarios.extend(real_scenarios)
    print(json.dumps(build_artifact(scenarios, docker=docker), indent=2))


if __name__ == "__main__":
    main()
