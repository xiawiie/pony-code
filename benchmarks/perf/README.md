# Pico Perf Benchmarks

Latency measurements for pico's hot paths. **Not CI-gated** — run
locally before/after a change to spot regressions.

## Usage

```bash
uv run python -m benchmarks.perf.bench_build_v2 > results-build_v2.json
uv run python -m benchmarks.perf.bench_retrieval > results-retrieval.json
uv run python -m benchmarks.perf.bench_recall > results-recall.json
```

Each script prints a JSON document with per-scenario `median_ns`,
`p95_ns`, and `min_ns`. Diff two runs to spot regressions.

## When to re-run

- After changing `FIELD_BOOSTS`, `LINK_MAX_ADDED`, `LINK_DECAY` in
  `pico/memory/retrieval.py`
- After adding or removing an injection source
- After changing the history budget algorithm

## Output shape

```json
{
  "scenarios": [
    {
      "name": "build_v2/small",
      "iterations": 100,
      "median_ns": 123456,
      "p95_ns": 234567,
      "min_ns": 100000
    }
  ]
}
```
