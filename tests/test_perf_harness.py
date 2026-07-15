"""Task D3: perf harness runs a benchmark and returns structured stats."""


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
