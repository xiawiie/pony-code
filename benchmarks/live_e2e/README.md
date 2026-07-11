# Live Provider End-to-End Harness

This standalone harness runs five designed Pico turns against one explicitly
selected Anthropic-compatible provider. It loads the project `.env` before
parsing options and writes a trace-backed JSON report under
`benchmarks/live_e2e/results/`.

Run exactly one provider gate per invocation; these commands are alternatives:

```bash
uv run python -m benchmarks.live_e2e.run_live_session --provider deepseek
uv run python -m benchmarks.live_e2e.run_live_session --provider anthropic
```

The A3 final authorized gate uses DeepSeek only. The existing five turns,
15-call cap, and 200,000 input-plus-output token cap are unchanged.

## Configuration

Use canonical project environment variables:

```text
PICO_DEEPSEEK_API_KEY / PICO_DEEPSEEK_MODEL / PICO_DEEPSEEK_API_BASE
PICO_ANTHROPIC_API_KEY / PICO_ANTHROPIC_MODEL / PICO_ANTHROPIC_API_BASE
```

The selected provider defaults to DeepSeek. `--model` overrides the selected
provider's configured model for that one run.

## What makes a run pass

The harness treats the persisted run trace as the source of truth for every
model call: usage totals, request metadata, action origins, and stable
`system_cache_key` evidence are collected per call. A missing or malformed
trace makes usage unknown and fails the gate; it never falls back to mutable
provider or session state. Token cache counters are observability data, not a
DeepSeek pass criterion.

Each turn must also have terminal `task_state.json`, `report.json`, and
`trace.jsonl` artifacts. The persisted session must be schema v3 with no
`history`, and canonical tool pairs must remain immediately adjacent.

Reports include the selected provider, model, git revision, assertions, and
trace evidence. They do not serialize environment dictionaries, provider
objects, request headers, or API keys.

Deterministic tests are the security source of truth. The live gate adds three
integration checks: every captured Provider payload excludes the selected API
key, every new or changed file under `.pico` excludes it, and active private
files/directories use 0600/0700 modes on POSIX. The project `.env` is
intentionally outside artifact scanning. Reports retain only safe relative
paths, counts, and modes; the whole payload is redacted and checked before its
private atomic write.

## Safety

- Pico runs with `read_only=True` and exposes only the native `read_file` tool.
- The fixture snapshots and restores `pico.toml` and removes its seed note.
- Sessions and run artifacts remain available for diagnosis after a run.
- This is a local integration harness, not an OS sandbox or network isolation
  boundary.
