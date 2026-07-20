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


def test_security_scenario_names_are_stable():
    from benchmarks.perf.bench_security import SCENARIO_NAMES

    assert SCENARIO_NAMES == (
        "security/redact_artifact/100",
        "shell/assess_corpus/50",
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
