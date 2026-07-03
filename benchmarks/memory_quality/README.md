# Memory Quality Benchmark

Release-gate benchmark for Pico memory v2. Not part of CI — costs real
LLM tokens per run, so trigger it manually before cutting a release.

## Run

```
python benchmarks/memory_quality/run_benchmark.py [--scenario <substring>]
```

The runner materializes a fresh temp workspace per scenario, seeds it
from `setup_notes`, and reports the expected success metric so a human
reviewer can score the LLM's response. The full model call + tool-trace
capture is scaffolded and will be wired to a live provider before the
first release; the harness structure (loader + workspace setup + summary
table) already runs end-to-end.

## Scenarios

1. **recall** — user tells a fact, agent should call `memory_search` to
   surface it later in the session.
2. **search_cn** — Chinese query must hit Chinese notes via CJK bigram
   tokenizer.
3. **update** — user explicitly asks the agent to remember something;
   agent should call `memory_save` (not re-create a duplicate note).
4. **multi_note** — a request that touches multiple domains should
   retrieve all relevant notes in the top hits.
5. **no_noise** — an off-topic user turn should not falsely trigger a
   high-scoring memory hit.

## Success gate

Per-scenario absolute thresholds live in
`docs/superpowers/specs/2026-07-02-pico-memory-v2-design.md` §13.2.
Reviewers compare the runner output against those thresholds by hand
during release triage.
