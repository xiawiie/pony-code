# Memory Quality Benchmark

Release-gate benchmark for Pico memory v2.

## Run

Deterministic local evidence:

```bash
python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

Optional live-provider evidence:

```bash
python benchmarks/memory_quality/run_benchmark.py --mode live --provider deepseek --format json
```

`--mode fake` uses a scripted fake model client and the real Pico runtime. It
proves that memory tools execute, trace artifacts are written, and scenario
scoring works without live provider credentials.

`--mode live` uses a configured real provider and is useful before release, but
it is not required for the fast local gate. Live-provider evidence depends on
provider credentials, quota, and model behavior.

## Output

JSON output has this stable top-level shape:

```json
{
  "schema_version": 1,
  "mode": "fake",
  "summary": {
    "total": 8,
    "passed": 8,
    "failed": 0,
    "pass_rate": 1.0
  },
  "rows": []
}
```

Rows include scenario id, pass/fail status, memory tool calls, expected hits,
observed hits, whether agent notes changed, and a failure reason.

The current fake benchmark reports 8 rows across the 5 scenario groups below.

## Scenarios

1. **recall** - the agent should call `memory_search` and surface the expected note.
2. **search_cn** - Chinese queries should hit Chinese notes via CJK bigram tokenizer.
3. **update** - explicit remember requests should call `memory_save`.
4. **multi_note** - multi-domain requests should retrieve all expected notes in the top hits.
5. **no_noise** - off-topic turns should avoid high-scoring irrelevant memory hits.
