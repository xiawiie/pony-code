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


def test_security_recovery_scenario_names_are_stable():
    from benchmarks.perf.bench_security_recovery import SCENARIO_NAMES

    assert SCENARIO_NAMES == (
        "security/redact_artifact/100",
        "shell/assess_corpus/50",
        "recovery/pending_reviews/200",
        "recovery/preview/100",
    )
