"""Report-only benchmarks for Docker Sandbox production owners."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import subprocess
import tempfile

from benchmarks.perf.harness import bench
from pico.docker_sandbox import (
    default_image_manifest_path,
    load_image_manifest,
    MOUNT_POLICY_DIGEST,
    RESOURCE_POLICY_DIGEST,
)
from pico.sandbox_session import SandboxSessionStore


SCENARIO_NAMES = (
    "sandbox/image_manifest/warm",
    "sandbox/session_inspect/warm",
    "sandbox/inventory_parallel/1",
    "sandbox/inventory_parallel/4",
    "sandbox/inventory_parallel/16",
)
ROOT = Path(__file__).resolve().parents[2]


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


def build_artifact(scenarios):
    image = load_image_manifest(default_image_manifest_path())
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
        },
        "sandbox": {
            "implementation": "docker_container",
            "image_digest": image.reference,
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


def _fixture(root):
    image = load_image_manifest(default_image_manifest_path())
    source = root / "source"
    source.mkdir()
    (source / "README.md").write_text("benchmark\n", encoding="utf-8")
    store = SandboxSessionStore(root / "sandboxes")
    session = store.create(
        source,
        pico_session_id="benchmark-session",
        bootstrap_git=_bootstrap,
        engine={
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "benchmark",
            "server_version": "benchmark",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        image={
            "reference": image.registry_reference or image.reference,
            "manifest_digest": image.reference,
            "image_id": image.image_id,
            "platform": image.platform,
        },
        policy={
            "version": 1,
            "digest": image.policy_digest,
            "network": "none",
            "mount_digest": MOUNT_POLICY_DIGEST,
            "resource_digest": RESOURCE_POLICY_DIGEST,
        },
    )
    return store, session


def main():
    with tempfile.TemporaryDirectory() as directory:
        store, session = _fixture(Path(directory).resolve())
        scenarios = [
            bench(
                SCENARIO_NAMES[0],
                lambda: load_image_manifest(default_image_manifest_path()),
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
    print(json.dumps(build_artifact(scenarios), indent=2))


if __name__ == "__main__":
    main()
