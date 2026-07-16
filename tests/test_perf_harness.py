"""Task D3: perf harness runs a benchmark and returns structured stats."""

from pathlib import Path


def test_bench_returns_stats():
    from benchmarks.perf.harness import bench

    result = bench("noop", lambda: sum(range(100)), iterations=10, warmup=2)
    assert result["name"] == "noop"
    assert result["iterations"] == 10
    assert isinstance(result["median_ns"], int)
    assert isinstance(result["p95_ns"], int)
    assert isinstance(result["min_ns"], int)
    assert result["min_ns"] > 0
    assert result["p95_ns"] >= result["median_ns"] >= result["min_ns"]


def test_bench_runs_cleanup_after_every_sample():
    from benchmarks.perf.harness import bench

    cleaned = []
    bench(
        "cleanup",
        lambda: len(cleaned),
        iterations=3,
        warmup=2,
        cleanup=cleaned.append,
    )

    assert len(cleaned) == 5


def test_security_recovery_scenario_names_are_stable():
    from benchmarks.perf.bench_security_recovery import SCENARIO_NAMES

    assert SCENARIO_NAMES == (
        "security/redact_artifact/100",
        "shell/assess_corpus/50",
        "recovery/pending_reviews/200",
        "recovery/preview/100",
    )


def test_sandbox_scenario_names_are_stable():
    from benchmarks.perf.bench_sandbox import SCENARIO_NAMES

    assert SCENARIO_NAMES == (
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


def test_session_context_performance_targets_are_stable():
    from benchmarks.perf import bench_session_context

    assert bench_session_context.SCENARIO_NAMES == (
        "session/load/10000/cold",
        "session/append/10000/warm",
        "compaction/plan/10000/warm",
        "context/build/compacted-10000/warm",
    )
    assert bench_session_context.SCENARIO_TARGETS == {
        "session/load/10000/cold": ("median_ns", 250_000_000),
        "session/append/10000/warm": ("p95_ns", 20_000_000),
        "compaction/plan/10000/warm": ("p95_ns", 100_000_000),
        "context/build/compacted-10000/warm": ("p95_ns", 50_000_000),
    }


def test_recall_benchmark_exposes_shared_snapshot_scenarios():
    from benchmarks.perf import bench_recall

    source = Path(bench_recall.__file__).read_text(encoding="utf-8")
    assert "memory/turn/512/shared_snapshot" in source
    assert "memory/turn/512/double_scan_reference" in source
    assert "scan_count_per_turn" in source


def test_sandbox_perf_artifact_has_release_provenance(monkeypatch):
    from benchmarks.perf import bench_sandbox

    values = {
        ("rev-parse", "HEAD"): "abc123",
        ("status", "--porcelain"): " M pico/example.py",
    }
    monkeypatch.setattr(
        bench_sandbox,
        "_git_value",
        lambda args: values[tuple(args)],
    )

    artifact = bench_sandbox.build_artifact([{"name": "sandbox/image_manifest/warm"}])

    assert artifact["suite"] == "sandbox-performance-report-only"
    assert artifact["runtime"]["commit"] == "abc123"
    assert artifact["runtime"]["dirty"] is True
    assert artifact["runtime"]["python"]
    assert artifact["runtime"]["platform"]
    assert artifact["runtime"]["architecture"]
    assert artifact["runtime"]["machine"]
    assert artifact["runtime"]["docker"] == {"status": "not_measured"}
    assert artifact["sandbox"] == {
        "implementation": "docker_container",
        "image_digest": "sha256:61f5e86e344d4053b8f6c7053c965b2cde7fc5e77777974e6237ad2e4ec36904",
        "policy_digest": "sha256:96aa648358b4e8efa83c5d1792b980518198844e7993893b65307c12a7a1c2f6",
        "network_mode": "none",
    }
    assert artifact["baseline"]["comparison"] == "report_only"
    assert artifact["scenarios"][0]["status"] == "measured"
