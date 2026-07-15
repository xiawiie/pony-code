"""Stdlib-only benchmark harness.

Each ``bench`` call warmup-runs, then measures ``iterations`` timings
with ``time.perf_counter_ns`` and returns median / p95 / min. No CI
gating: users run these scripts manually and compare JSON output
across code changes.
"""

from __future__ import annotations

import statistics
import time


def _percentile(samples, p):
    if not samples:
        return 0
    sorted_samples = sorted(samples)
    k = (len(sorted_samples) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_samples) - 1)
    d = k - f
    return int(sorted_samples[f] + (sorted_samples[c] - sorted_samples[f]) * d)


def bench(name, fn, iterations=100, warmup=5, cleanup=None):
    """Time ``fn`` and return structured stats.

    Warmup runs are discarded. Optional cleanup runs after timing each call.
    Measured samples are collected via ``time.perf_counter_ns``. Returns a
    dict with keys ``name``, ``iterations``, ``median_ns``, ``p95_ns``,
    ``min_ns``.
    """
    for _ in range(warmup):
        result = fn()
        if cleanup is not None:
            cleanup(result)
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        result = fn()
        samples.append(time.perf_counter_ns() - t0)
        if cleanup is not None:
            cleanup(result)
    return {
        "name": name,
        "iterations": iterations,
        "median_ns": int(statistics.median(samples)),
        "p95_ns": _percentile(samples, 95),
        "min_ns": min(samples),
    }
