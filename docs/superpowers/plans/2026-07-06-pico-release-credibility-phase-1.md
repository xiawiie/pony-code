# Pico Release Credibility Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 1 of the release credibility upgrade: provider benchmark selection plus deterministic memory-quality tool-trace scoring.

**Architecture:** Keep provider benchmark changes inside the existing `pico.evaluation.provider_benchmark` module and script wrapper. Rewrite the memory-quality runner as a small benchmark harness that can run deterministic fake-model scenarios through the real Pico runtime, read `trace.jsonl`, score memory tool behavior, and produce stable JSON. Do not implement performance gates, `run --format json`, or all-provider doctor diagnostics in this phase.

**Tech Stack:** Python stdlib, pytest, uv, Pico runtime, `FakeModelClient`, `RunStore` trace artifacts, existing memory v2 tools.

---

## Scope Guard

This plan implements only:

1. Provider Control Pack
2. Memory Evidence Pack

This plan does not implement:

- `scripts/check_memory_perf.py`
- `scripts/check_release.sh`
- `pico-cli --format json run <prompt>`
- `pico-cli doctor --all-providers`

## File Structure

- Modify: `pico/evaluation/provider_benchmark.py`
  - Owns provider benchmark provider selection and the real provider loop.
  - Add `_normalize_provider_selection()` and a `providers` parameter to `run_provider_experiments()`.

- Modify: `scripts/run_provider_experiments.py`
  - Owns CLI arguments for the provider benchmark script.
  - Add `--provider all|gpt|claude|deepseek`.

- Modify: `benchmarks/memory_quality/run_benchmark.py`
  - Owns scenario loading, temp workspace setup, fake/live model client construction, trace parsing, scoring, output formatting, and exit status for memory quality.
  - Keep it self-contained because it is a release benchmark script, not a reusable runtime module.

- Modify: `benchmarks/memory_quality/README.md`
  - Replace scaffold/manual-scoring language with the new deterministic and optional-live mode contract.

- Modify: `docs/review-pack/README.md`
  - Replace the scaffold caveat with current Phase 1 evidence commands and caveats.

- Modify: `tests/test_scripts.py`
  - Add provider benchmark parser tests.

- Modify: `tests/test_metrics.py`
  - Add provider benchmark selection tests around `run_provider_experiments()`.

- Create: `tests/test_memory_quality_benchmark.py`
  - Test the memory-quality runner without live provider credentials.

## Task 1: Provider Benchmark Selection Tests

**Files:**
- Modify: `tests/test_scripts.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Add parser tests for `--provider`**

Append this test to `tests/test_scripts.py`:

```python
def test_provider_experiment_parser_accepts_provider_selector():
    spec = importlib.util.spec_from_file_location(
        "run_provider_experiments_script",
        Path("scripts/run_provider_experiments.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    default_args = module.build_arg_parser().parse_args(["--output-json", "out.json"])
    selected_args = module.build_arg_parser().parse_args(
        ["--output-json", "out.json", "--provider", "deepseek"]
    )

    assert default_args.provider == "all"
    assert selected_args.provider == "deepseek"
```

- [ ] **Step 2: Run parser test and verify it fails**

Run:

```bash
uv run pytest tests/test_scripts.py::test_provider_experiment_parser_accepts_provider_selector -q
```

Expected: FAIL with `AttributeError: 'Namespace' object has no attribute 'provider'`.

- [ ] **Step 3: Add provider benchmark selection tests**

Append these imports to the top of `tests/test_metrics.py` if they are not already present:

```python
import pytest
```

Append these tests to `tests/test_metrics.py`:

```python
def test_provider_selection_normalizes_default_all_and_single_provider():
    from pico.evaluation.provider_benchmark import _normalize_provider_selection

    assert _normalize_provider_selection(None) == ("gpt", "claude", "deepseek")
    assert _normalize_provider_selection("all") == ("gpt", "claude", "deepseek")
    assert _normalize_provider_selection("deepseek") == ("deepseek",)
    assert _normalize_provider_selection(["gpt", "deepseek"]) == ("gpt", "deepseek")


def test_provider_selection_rejects_unknown_provider():
    from pico.evaluation.provider_benchmark import _normalize_provider_selection

    with pytest.raises(ValueError, match="unknown provider"):
        _normalize_provider_selection("openai")


def test_run_provider_experiments_targets_selected_provider(tmp_path, monkeypatch):
    from pico.evaluation.provider_benchmark import run_provider_experiments

    seen = []

    def fake_provider_profile(provider):
        seen.append(provider)
        return {
            "provider": provider,
            "status": "blocked",
            "reason": f"{provider} key missing",
        }

    monkeypatch.setattr(
        "pico.evaluation.provider_benchmark._provider_profile",
        fake_provider_profile,
    )

    payload = run_provider_experiments(
        benchmark_path=tmp_path / "benchmarks.json",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
        providers="deepseek",
    )

    assert seen == ["deepseek"]
    assert payload == {
        "providers": [
            {
                "provider": "deepseek",
                "status": "blocked",
                "reason": "deepseek key missing",
            }
        ]
    }


def test_run_provider_experiments_default_keeps_three_provider_order(tmp_path, monkeypatch):
    from pico.evaluation.provider_benchmark import run_provider_experiments

    seen = []

    def fake_provider_profile(provider):
        seen.append(provider)
        return {
            "provider": provider,
            "status": "blocked",
            "reason": f"{provider} key missing",
        }

    monkeypatch.setattr(
        "pico.evaluation.provider_benchmark._provider_profile",
        fake_provider_profile,
    )

    payload = run_provider_experiments(
        benchmark_path=tmp_path / "benchmarks.json",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
    )

    assert seen == ["gpt", "claude", "deepseek"]
    assert [row["provider"] for row in payload["providers"]] == [
        "gpt",
        "claude",
        "deepseek",
    ]
```

- [ ] **Step 4: Run provider benchmark tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_metrics.py::test_provider_selection_normalizes_default_all_and_single_provider \
  tests/test_metrics.py::test_provider_selection_rejects_unknown_provider \
  tests/test_metrics.py::test_run_provider_experiments_targets_selected_provider \
  tests/test_metrics.py::test_run_provider_experiments_default_keeps_three_provider_order \
  -q
```

Expected: FAIL with an import error for `_normalize_provider_selection` and a signature error for the `providers` keyword.

## Task 2: Provider Benchmark Selection Implementation

**Files:**
- Modify: `pico/evaluation/provider_benchmark.py`
- Modify: `scripts/run_provider_experiments.py`
- Test: `tests/test_scripts.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Add provider selection helper**

In `pico/evaluation/provider_benchmark.py`, insert this near `DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS`:

```python
PROVIDER_BENCHMARK_CHOICES = ("gpt", "claude", "deepseek")


def _normalize_provider_selection(providers=None):
    if providers is None:
        return PROVIDER_BENCHMARK_CHOICES
    if isinstance(providers, str):
        requested = [providers]
    else:
        requested = list(providers)

    normalized = []
    for provider in requested:
        provider_name = str(provider).strip().lower()
        if not provider_name:
            continue
        if provider_name == "all":
            return PROVIDER_BENCHMARK_CHOICES
        if provider_name not in PROVIDER_BENCHMARK_CHOICES:
            choices = ", ".join(("all", *PROVIDER_BENCHMARK_CHOICES))
            raise ValueError(f"unknown provider: {provider_name}. expected one of: {choices}")
        if provider_name not in normalized:
            normalized.append(provider_name)

    if not normalized:
        return PROVIDER_BENCHMARK_CHOICES
    return tuple(normalized)
```

- [ ] **Step 2: Thread provider selection through `run_provider_experiments()`**

Change the function signature and loop in `pico/evaluation/provider_benchmark.py` to this shape:

```python
def run_provider_experiments(
    benchmark_path,
    workspace_root,
    artifact_root,
    max_new_tokens=DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS,
    providers=None,
):
    benchmark_path = Path(benchmark_path)
    workspace_root = Path(workspace_root)
    artifact_root = Path(artifact_root)
    provider_rows = []
    for provider_name in _normalize_provider_selection(providers):
        profile = _provider_profile(provider_name)
        if profile["status"] != "ready":
            provider_rows.append(profile)
            continue
        if provider_name == "gpt":

            def factory(task, workspace, profile=profile):
                del task, workspace
                return OpenAICompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
                    timeout=300,
                )

        else:

            def factory(task, workspace, profile=profile):
                del task, workspace
                return AnthropicCompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
                    timeout=300,
                )

        artifact_path = artifact_root / f"{provider_name}-benchmark.json"
        try:
            payload = run_fixed_benchmark(
                benchmark_path=benchmark_path,
                artifact_path=artifact_path,
                workspace_root=workspace_root / provider_name,
                model_name=profile["provider"],
                model_version=profile["model"],
                max_new_tokens=max_new_tokens,
                model_client_factory=factory,
            )
            payload["_artifact_path"] = str(artifact_path)
            result = _provider_summary_from_artifact(payload)
            result["provider"] = provider_name
            result["model"] = profile["model"]
            provider_rows.append(result)
        except Exception as exc:
            provider_rows.append(
                {
                    "provider": provider_name,
                    "status": "error",
                    "model": profile["model"],
                    "reason": str(exc),
                }
            )
    return {"providers": provider_rows}
```

- [ ] **Step 3: Add CLI script argument**

In `scripts/run_provider_experiments.py`, add this parser argument after `--output-json`:

```python
    parser.add_argument(
        "--provider",
        choices=("all", "gpt", "claude", "deepseek"),
        default="all",
        help="Provider benchmark target. Use 'all' to run GPT, Claude, and DeepSeek.",
    )
```

Then pass it into `run_provider_experiments()`:

```python
    payload = run_provider_experiments(
        benchmark_path=args.benchmark_path,
        workspace_root=args.workspace_root,
        artifact_root=args.artifact_root,
        max_new_tokens=args.max_new_tokens,
        providers=args.provider,
    )
```

- [ ] **Step 4: Run provider selector tests**

Run:

```bash
uv run pytest \
  tests/test_scripts.py::test_provider_experiment_parser_accepts_provider_selector \
  tests/test_metrics.py::test_provider_selection_normalizes_default_all_and_single_provider \
  tests/test_metrics.py::test_provider_selection_rejects_unknown_provider \
  tests/test_metrics.py::test_run_provider_experiments_targets_selected_provider \
  tests/test_metrics.py::test_run_provider_experiments_default_keeps_three_provider_order \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run script help smoke**

Run:

```bash
uv run python scripts/run_provider_experiments.py --help
```

Expected: exit code 0 and output contains `--provider {all,gpt,claude,deepseek}`.

- [ ] **Step 6: Commit provider selector**

Run:

```bash
git add pico/evaluation/provider_benchmark.py scripts/run_provider_experiments.py tests/test_scripts.py tests/test_metrics.py
git commit -m "feat: target provider benchmark runs"
```

## Task 3: Memory Quality Loader and Workspace Tests

**Files:**
- Create: `tests/test_memory_quality_benchmark.py`
- Modify: `benchmarks/memory_quality/run_benchmark.py`

- [ ] **Step 1: Create a module loader and loader tests**

Create `tests/test_memory_quality_benchmark.py` with this content:

```python
import importlib.util
import json
from pathlib import Path

import pytest


def _load_memory_benchmark_module():
    spec = importlib.util.spec_from_file_location(
        "memory_quality_benchmark",
        Path("benchmarks/memory_quality/run_benchmark.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_scenarios_reports_json_file_and_line(tmp_path):
    module = _load_memory_benchmark_module()
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "scenario_bad.jsonl").write_text('{"id": "bad"\n', encoding="utf-8")

    with pytest.raises(module.ScenarioLoadError, match=r"scenario_bad\.jsonl:1"):
        list(module.load_scenarios(scenario_dir=scenario_dir))


def test_setup_workspace_rejects_invalid_setup_path(tmp_path):
    module = _load_memory_benchmark_module()

    with pytest.raises(ValueError, match="invalid setup note path"):
        module.setup_workspace(
            {
                "id": "bad_path",
                "setup_notes": {"../outside.md": "secret"},
                "session_turns": [],
            },
            parent_dir=tmp_path,
        )


def test_setup_workspace_maps_workspace_notes_to_pico_memory(tmp_path):
    module = _load_memory_benchmark_module()

    ws = module.setup_workspace(
        {
            "id": "recall_bcrypt",
            "setup_notes": {
                "workspace/notes/auth.md": "# Auth\n\n- bcrypt rounds must be <= 12\n",
                "workspace/agent_notes.md": "- old lesson\n",
            },
            "session_turns": [],
        },
        parent_dir=tmp_path,
    )

    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "# Test project\n"
    assert (
        ws / ".pico" / "memory" / "notes" / "auth.md"
    ).read_text(encoding="utf-8") == "# Auth\n\n- bcrypt rounds must be <= 12\n"
    assert (
        ws / ".pico" / "memory" / "agent_notes.md"
    ).read_text(encoding="utf-8") == "- old lesson\n"
```

- [ ] **Step 2: Run loader tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_load_scenarios_reports_json_file_and_line \
  tests/test_memory_quality_benchmark.py::test_setup_workspace_rejects_invalid_setup_path \
  tests/test_memory_quality_benchmark.py::test_setup_workspace_maps_workspace_notes_to_pico_memory \
  -q
```

Expected: FAIL because `ScenarioLoadError`, `scenario_dir`, and `parent_dir` support do not exist.

- [ ] **Step 3: Implement strict loader and workspace setup**

Replace the top-level loader/setup section in `benchmarks/memory_quality/run_benchmark.py` with this code:

```python
SCHEMA_VERSION = 1
SCENARIO_DIR = Path(__file__).parent
VALID_MODES = ("fake", "live")
VALID_FORMATS = ("text", "json")
VALID_LIVE_PROVIDERS = ("gpt", "claude", "deepseek")


class ScenarioLoadError(ValueError):
    pass


def load_scenarios(filter_id: str | None = None, scenario_dir: Path = SCENARIO_DIR):
    scenario_dir = Path(scenario_dir)
    for jsonl in sorted(scenario_dir.glob("scenario_*.jsonl")):
        for line_number, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScenarioLoadError(f"{jsonl}:{line_number}: invalid JSON: {exc.msg}") from exc
            if filter_id and filter_id not in str(data.get("id", "")):
                continue
            scenario_id = str(data.get("id", "")).strip()
            if not scenario_id:
                raise ScenarioLoadError(f"{jsonl}:{line_number}: scenario id must not be empty")
            if not isinstance(data.get("session_turns"), list):
                raise ScenarioLoadError(f"{jsonl}:{line_number}: session_turns must be a list")
            yield jsonl.stem, data


def _setup_note_target(workspace: Path, rel_path: str) -> Path:
    parts = str(rel_path).split("/", 1)
    if len(parts) != 2 or parts[0] != "workspace":
        raise ValueError(f"invalid setup note path: {rel_path}")
    sub_path = parts[1]
    if not sub_path or sub_path.startswith("/") or ".." in sub_path.split("/"):
        raise ValueError(f"invalid setup note path: {rel_path}")
    if sub_path == "agent_notes.md":
        return workspace / ".pico" / "memory" / "agent_notes.md"
    if sub_path.startswith("notes/") and sub_path.endswith(".md"):
        return workspace / ".pico" / "memory" / sub_path
    raise ValueError(f"invalid setup note path: {rel_path}")


def setup_workspace(scenario: dict, parent_dir: Path | None = None) -> Path:
    parent_dir = Path(parent_dir) if parent_dir is not None else None
    ws = Path(tempfile.mkdtemp(prefix="pico-memory-bench-", dir=str(parent_dir) if parent_dir else None))
    (ws / "AGENTS.md").write_text("# Test project\n", encoding="utf-8")
    setup_notes = scenario.get("setup_notes", {})
    if not isinstance(setup_notes, dict):
        raise ValueError(f"{scenario.get('id', '<unknown>')}: setup_notes must be an object")
    for rel, content in setup_notes.items():
        target = _setup_note_target(ws, str(rel))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return ws
```

- [ ] **Step 4: Run loader tests again**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_load_scenarios_reports_json_file_and_line \
  tests/test_memory_quality_benchmark.py::test_setup_workspace_rejects_invalid_setup_path \
  tests/test_memory_quality_benchmark.py::test_setup_workspace_maps_workspace_notes_to_pico_memory \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit loader and workspace setup**

Run:

```bash
git add benchmarks/memory_quality/run_benchmark.py tests/test_memory_quality_benchmark.py
git commit -m "test: cover memory quality scenario setup"
```

## Task 4: Memory Quality Scoring Tests

**Files:**
- Modify: `tests/test_memory_quality_benchmark.py`
- Modify: `benchmarks/memory_quality/run_benchmark.py`

- [ ] **Step 1: Add trace parsing and scoring tests**

Append these tests to `tests/test_memory_quality_benchmark.py`:

```python
def test_parse_memory_search_hits_extracts_paths_and_scores():
    module = _load_memory_benchmark_module()

    hits = module.parse_memory_search_hits(
        "Found 2 match(es) for 'bcrypt':\n"
        "- workspace/notes/auth.md (score=1.23)\n"
        "  L3: bcrypt rounds\n"
        "- workspace/notes/session.md (score=0.75)\n"
    )

    assert hits == [
        {"path": "workspace/notes/auth.md", "score": 1.23},
        {"path": "workspace/notes/session.md", "score": 0.75},
    ]


def test_score_recall_requires_expected_hit_in_top_three(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "recall_bcrypt",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "how should login hashing work?",
                "expected_search_hit": "workspace/notes/auth.md",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "hashing", "limit": 5},
            "result": "- workspace/notes/auth.md (score=1.23)\n",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_search"]
    assert row["expected_hits"] == ["workspace/notes/auth.md"]
    assert row["observed_hits"] == ["workspace/notes/auth.md"]
    assert row["failure_reason"] == ""


def test_score_update_requires_memory_save_and_preserves_existing_note(tmp_path):
    module = _load_memory_benchmark_module()
    memory_root = tmp_path / ".pico" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").write_text(
        "- old lesson\n- bcrypt rounds > 12 causes CI timeout\n",
        encoding="utf-8",
    )
    scenario = {
        "id": "update_bcrypt_lesson",
        "setup_notes": {"workspace/agent_notes.md": "- old lesson\n"},
        "session_turns": [
            {
                "user": "please remember: bcrypt rounds > 12 causes CI timeout",
                "expected_tool": "memory_save",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_save",
            "args": {"note": "bcrypt rounds > 12 causes CI timeout"},
            "result": "saved: workspace/agent_notes.md (chars_total=80)",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_save"]
    assert row["agent_notes_changed"] is True
    assert row["failure_reason"] == ""


def test_score_no_noise_passes_without_memory_search(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "no_noise_food",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "what is for lunch?",
                "expected_no_search_hit": True,
            }
        ],
    }

    row = module.score_scenario(scenario, [], tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == []
    assert row["observed_hits"] == []


def test_score_no_noise_fails_on_high_scoring_irrelevant_hit(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "no_noise_food",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "what is for lunch?",
                "expected_no_search_hit": True,
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "lunch"},
            "result": "- workspace/notes/auth.md (score=1.25)\n",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "fail"
    assert row["failure_reason"] == "unexpected high-scoring memory hit"
```

- [ ] **Step 2: Run scoring tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_parse_memory_search_hits_extracts_paths_and_scores \
  tests/test_memory_quality_benchmark.py::test_score_recall_requires_expected_hit_in_top_three \
  tests/test_memory_quality_benchmark.py::test_score_update_requires_memory_save_and_preserves_existing_note \
  tests/test_memory_quality_benchmark.py::test_score_no_noise_passes_without_memory_search \
  tests/test_memory_quality_benchmark.py::test_score_no_noise_fails_on_high_scoring_irrelevant_hit \
  -q
```

Expected: FAIL because `parse_memory_search_hits()` and `score_scenario()` do not exist.

- [ ] **Step 3: Implement trace hit parsing and scoring helpers**

Add `import re` with the stdlib imports at the top of
`benchmarks/memory_quality/run_benchmark.py`. Then add these helpers after
`setup_workspace()`:

```python
_MEMORY_HIT_RE = re.compile(r"^- (?P<path>[a-z]+/[A-Za-z0-9_./-]+) \(score=(?P<score>[0-9.]+)\)")


def parse_memory_search_hits(result: str) -> list[dict]:
    hits = []
    for line in str(result or "").splitlines():
        match = _MEMORY_HIT_RE.match(line.strip())
        if not match:
            continue
        hits.append(
            {
                "path": match.group("path"),
                "score": float(match.group("score")),
            }
        )
    return hits


def _tool_events(trace_events: list[dict]) -> list[dict]:
    return [
        event
        for event in trace_events
        if event.get("event") == "tool_executed"
    ]


def _expected_note_from_turn(turn: dict) -> str:
    user = str(turn.get("user", "")).strip()
    marker = "please remember:"
    if user.lower().startswith(marker):
        return user[len(marker):].strip()
    return user


def _agent_notes_text(workspace: Path) -> str:
    path = Path(workspace) / ".pico" / "memory" / "agent_notes.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _seeded_agent_notes_text(scenario: dict) -> str:
    return str((scenario.get("setup_notes") or {}).get("workspace/agent_notes.md", ""))


def _score_expected_hits(row: dict, expected_hits: list[str], search_events: list[dict]) -> None:
    observed = []
    for event in search_events:
        observed.extend(hit["path"] for hit in parse_memory_search_hits(event.get("result", "")))
    row["expected_hits"] = expected_hits
    row["observed_hits"] = observed
    missing = [path for path in expected_hits if path not in observed[:3]]
    if missing:
        row["status"] = "fail"
        row["failure_reason"] = "missing expected memory hit: " + ", ".join(missing)


def _score_no_noise(row: dict, search_events: list[dict]) -> None:
    hits = []
    for event in search_events:
        hits.extend(parse_memory_search_hits(event.get("result", "")))
    row["observed_hits"] = [hit["path"] for hit in hits]
    if any(hit["score"] >= 1.0 for hit in hits):
        row["status"] = "fail"
        row["failure_reason"] = "unexpected high-scoring memory hit"


def _score_memory_save(row: dict, scenario: dict, turn: dict, tool_events: list[dict], workspace: Path) -> None:
    save_events = [event for event in tool_events if event.get("name") == "memory_save"]
    notes_text = _agent_notes_text(workspace)
    expected_note = _expected_note_from_turn(turn)
    seeded_text = _seeded_agent_notes_text(scenario)
    row["agent_notes_changed"] = bool(notes_text and notes_text != seeded_text)
    if not save_events:
        row["status"] = "fail"
        row["failure_reason"] = "memory_save was not called"
        return
    if expected_note and expected_note not in notes_text:
        row["status"] = "fail"
        row["failure_reason"] = "expected note was not saved"
        return
    if seeded_text and seeded_text.strip() and seeded_text.strip() not in notes_text:
        row["status"] = "fail"
        row["failure_reason"] = "existing agent note was not preserved"


def score_scenario(scenario: dict, trace_events: list[dict], workspace: Path) -> dict:
    tool_events = _tool_events(trace_events)
    tool_calls = [str(event.get("name", "")) for event in tool_events if event.get("name")]
    row = {
        "id": str(scenario.get("id", "")),
        "status": "pass",
        "tool_calls": tool_calls,
        "expected_hits": [],
        "observed_hits": [],
        "agent_notes_changed": False,
        "failure_reason": "",
    }
    search_events = [event for event in tool_events if event.get("name") == "memory_search"]

    for turn in scenario.get("session_turns", []):
        if turn.get("expected_no_search_hit"):
            _score_no_noise(row, search_events)
        expected_hit = turn.get("expected_search_hit")
        if expected_hit:
            _score_expected_hits(row, [str(expected_hit)], search_events)
        expected_hits_top = turn.get("expected_search_hits_top")
        if expected_hits_top:
            _score_expected_hits(row, [str(path) for path in expected_hits_top], search_events)
        if turn.get("expected_tool") == "memory_save":
            _score_memory_save(row, scenario, turn, tool_events, Path(workspace))
    return row
```

- [ ] **Step 4: Run scoring tests again**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_parse_memory_search_hits_extracts_paths_and_scores \
  tests/test_memory_quality_benchmark.py::test_score_recall_requires_expected_hit_in_top_three \
  tests/test_memory_quality_benchmark.py::test_score_update_requires_memory_save_and_preserves_existing_note \
  tests/test_memory_quality_benchmark.py::test_score_no_noise_passes_without_memory_search \
  tests/test_memory_quality_benchmark.py::test_score_no_noise_fails_on_high_scoring_irrelevant_hit \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit scoring helpers**

Run:

```bash
git add benchmarks/memory_quality/run_benchmark.py tests/test_memory_quality_benchmark.py
git commit -m "test: score memory quality trace evidence"
```

## Task 5: Memory Quality Fake Runner

**Files:**
- Modify: `tests/test_memory_quality_benchmark.py`
- Modify: `benchmarks/memory_quality/run_benchmark.py`

- [ ] **Step 1: Add fake-mode integration tests**

Append these tests to `tests/test_memory_quality_benchmark.py`:

```python
def test_fake_mode_outputs_json_summary_without_human_text(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "recall_bcrypt"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.out.lstrip().startswith("{")
    assert "=== summary ===" not in captured.out
    assert payload["schema_version"] == 1
    assert payload["mode"] == "fake"
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["failed"] == 0
    assert payload["rows"][0]["id"] == "recall_bcrypt"
    assert payload["rows"][0]["status"] == "pass"
    assert "memory_search" in payload["rows"][0]["tool_calls"]


def test_fake_mode_scores_update_scenario(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "update_bcrypt_lesson"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_save"]
    assert row["agent_notes_changed"] is True


def test_fake_mode_scores_no_noise_without_memory_search(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "no_noise_food"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["status"] == "pass"
    assert row["tool_calls"] == []
    assert row["observed_hits"] == []
```

- [ ] **Step 2: Run fake-mode tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_fake_mode_outputs_json_summary_without_human_text \
  tests/test_memory_quality_benchmark.py::test_fake_mode_scores_update_scenario \
  tests/test_memory_quality_benchmark.py::test_fake_mode_scores_no_noise_without_memory_search \
  -q
```

Expected: FAIL because `main(argv)` and fake runtime execution are not implemented.

- [ ] **Step 3: Add runtime imports**

At the top of `benchmarks/memory_quality/run_benchmark.py`, keep the existing stdlib imports and add these Pico imports after the `ROOT` sys.path block. If the script does not yet have a `ROOT` block, add one before these imports:

```python
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.evaluation.provider_benchmark import _make_provider_client  # noqa: E402
from pico.providers.clients import FakeModelClient  # noqa: E402
from pico.runtime import Pico, SessionStore  # noqa: E402
from pico.workspace import WorkspaceContext  # noqa: E402
```

- [ ] **Step 4: Implement scripted fake outputs and agent construction**

Add this code after the scoring helpers in `benchmarks/memory_quality/run_benchmark.py`:

```python
def _tool_call(name: str, args: dict) -> str:
    return "<tool>" + json.dumps({"name": name, "args": args}, sort_keys=True) + "</tool>"


def _fake_search_query_for_turn(turn: dict) -> str:
    user = str(turn.get("user", "")).strip()
    expected_paths = []
    if turn.get("expected_search_hit"):
        expected_paths.append(str(turn["expected_search_hit"]))
    if turn.get("expected_search_hits_top"):
        expected_paths.extend(str(path) for path in turn["expected_search_hits_top"])
    stems = " ".join(Path(path).stem for path in expected_paths)
    return " ".join(part for part in (user, stems) if part)


def _fake_outputs_for_turn(turn: dict) -> list[str]:
    if turn.get("expected_no_search_hit"):
        return ["<final>No relevant memory needed.</final>"]
    if turn.get("expected_tool") == "memory_save":
        return [
            _tool_call("memory_save", {"note": _expected_note_from_turn(turn)}),
            "<final>Saved the memory note.</final>",
        ]
    return [
        _tool_call("memory_search", {"query": _fake_search_query_for_turn(turn), "limit": 5}),
        "<final>Checked memory.</final>",
    ]


def _fake_outputs_for_scenario(scenario: dict) -> list[str]:
    outputs = []
    for turn in scenario.get("session_turns", []):
        outputs.extend(_fake_outputs_for_turn(turn))
    return outputs


def _build_model_client(mode: str, provider: str, scenario: dict):
    if mode == "fake":
        return FakeModelClient(_fake_outputs_for_scenario(scenario))
    return _make_provider_client(provider)


def _build_agent(workspace: Path, model_client) -> Pico:
    workspace_context = WorkspaceContext.build(str(workspace))
    return Pico(
        model_client=model_client,
        workspace=workspace_context,
        session_store=SessionStore(str(workspace / ".pico" / "sessions")),
        approval_policy="never",
        max_steps=8,
        max_new_tokens=512,
        depth=1,
    )


def _read_latest_trace(agent: Pico) -> list[dict]:
    if agent.current_task_state is None:
        return []
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    return [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
```

- [ ] **Step 5: Implement scenario execution and payload formatting**

Replace the existing `run_scenario()` and `main()` functions in `benchmarks/memory_quality/run_benchmark.py` with this code:

```python
def run_scenario(scenario_name: str, scenario: dict, keep: bool, mode: str, provider: str) -> dict:
    del scenario_name
    ws = None
    try:
        ws = setup_workspace(scenario)
        model_client = _build_model_client(mode, provider, scenario)
        agent = _build_agent(ws, model_client)
        trace_events = []
        for turn in scenario.get("session_turns", []):
            agent.ask(str(turn.get("user", "")).strip())
            trace_events.extend(_read_latest_trace(agent))
        row = score_scenario(scenario, trace_events, ws)
        if keep and ws is not None:
            row["workspace"] = str(ws)
        return row
    except Exception as exc:
        row = {
            "id": str(scenario.get("id", "")),
            "status": "fail",
            "tool_calls": [],
            "expected_hits": [],
            "observed_hits": [],
            "agent_notes_changed": False,
            "failure_reason": f"{type(exc).__name__}: {exc}",
        }
        if keep and ws is not None:
            row["workspace"] = str(ws)
        return row
    finally:
        if not keep and ws is not None:
            shutil.rmtree(ws, ignore_errors=True)


def summarize_rows(rows: list[dict]) -> dict:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("status") == "pass")
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / total if total else 0.0,
    }


def build_payload(rows: list[dict], mode: str, provider: str) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "summary": summarize_rows(rows),
        "rows": rows,
    }
    if mode == "live":
        payload["provider"] = provider
    return payload


def render_text(payload: dict) -> str:
    lines = ["=== summary ==="]
    summary = payload["summary"]
    lines.append(
        f"total={summary['total']} passed={summary['passed']} failed={summary['failed']} "
        f"pass_rate={summary['pass_rate']:.3f}"
    )
    for row in payload["rows"]:
        suffix = ""
        if row.get("failure_reason"):
            suffix = f" - {row['failure_reason']}"
        lines.append(f"  {row['id']}: {row['status']}{suffix}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default=None, help="Filter by id substring.")
    parser.add_argument("--keep", action="store_true", help="Keep temp workspaces.")
    parser.add_argument("--mode", choices=VALID_MODES, default="fake")
    parser.add_argument("--provider", choices=VALID_LIVE_PROVIDERS, default="deepseek")
    parser.add_argument("--format", choices=VALID_FORMATS, default="text")
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    rows = []
    try:
        scenarios = list(load_scenarios(filter_id=args.scenario))
    except ScenarioLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not scenarios:
        print("no memory quality scenarios matched", file=sys.stderr)
        return 1

    for stem, scenario in scenarios:
        row = run_scenario(stem, scenario, keep=args.keep, mode=args.mode, provider=args.provider)
        rows.append(row)
        if args.fail_fast and row.get("status") != "pass":
            break

    payload = build_payload(rows, mode=args.mode, provider=args.provider)
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload))
    return 0 if payload["summary"]["total"] and payload["summary"]["failed"] == 0 else 1
```

- [ ] **Step 6: Ensure script entrypoint passes argv through `main()`**

At the bottom of `benchmarks/memory_quality/run_benchmark.py`, keep:

```python
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Run fake-mode tests**

Run:

```bash
uv run pytest \
  tests/test_memory_quality_benchmark.py::test_fake_mode_outputs_json_summary_without_human_text \
  tests/test_memory_quality_benchmark.py::test_fake_mode_scores_update_scenario \
  tests/test_memory_quality_benchmark.py::test_fake_mode_scores_no_noise_without_memory_search \
  -q
```

Expected: PASS.

- [ ] **Step 8: Run all memory-quality tests**

Run:

```bash
uv run pytest tests/test_memory_quality_benchmark.py -q
```

Expected: PASS.

- [ ] **Step 9: Run deterministic benchmark command**

Run:

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

Expected: exit code 0. Output is one JSON object with `"schema_version": 1`, `"mode": "fake"`, and `"failed": 0` inside the summary.

- [ ] **Step 10: Commit fake runner**

Run:

```bash
git add benchmarks/memory_quality/run_benchmark.py tests/test_memory_quality_benchmark.py
git commit -m "feat: score memory quality traces"
```

## Task 6: Phase 1 Documentation Updates

**Files:**
- Modify: `benchmarks/memory_quality/README.md`
- Modify: `docs/review-pack/README.md`

- [ ] **Step 1: Update memory-quality README**

Replace the content of `benchmarks/memory_quality/README.md` with:

````markdown
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
it is not required for the fast local gate.

## Output

JSON output has this stable top-level shape:

```json
{
  "schema_version": 1,
  "mode": "fake",
  "summary": {
    "total": 5,
    "passed": 5,
    "failed": 0,
    "pass_rate": 1.0
  },
  "rows": []
}
```

Rows include scenario id, pass/fail status, memory tool calls, expected hits,
observed hits, whether agent notes changed, and a failure reason.

## Scenarios

1. **recall** - the agent should call `memory_search` and surface the expected note.
2. **search_cn** - Chinese queries should hit Chinese notes via CJK bigram tokenizer.
3. **update** - explicit remember requests should call `memory_save`.
4. **multi_note** - multi-domain requests should retrieve all expected notes in the top hits.
5. **no_noise** - off-topic turns should avoid high-scoring irrelevant memory hits.
````

- [ ] **Step 2: Update review-pack caveat**

In `docs/review-pack/README.md`, replace the existing memory-quality caveat paragraph with:

```markdown
Memory quality evidence: `benchmarks/memory_quality/run_benchmark.py --mode fake --format json` runs deterministic tool-trace scoring through the real Pico runtime. Live-provider memory-quality evidence remains optional because it depends on provider credentials, quota, and model behavior.
```

- [ ] **Step 3: Run docs grep checks**

Run:

```bash
rg -n "scaffold_only|manual scoring|human reviewer can score|full model call \\+ tool-trace capture is scaffolded" benchmarks/memory_quality docs/review-pack
```

Expected: no output and exit code 1.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add benchmarks/memory_quality/README.md docs/review-pack/README.md
git commit -m "docs: describe memory quality trace scoring"
```

## Task 7: Phase 1 Final Verification

**Files:**
- Verify: `pico/evaluation/provider_benchmark.py`
- Verify: `scripts/run_provider_experiments.py`
- Verify: `benchmarks/memory_quality/run_benchmark.py`
- Verify: `benchmarks/memory_quality/README.md`
- Verify: `docs/review-pack/README.md`
- Verify: `tests/test_scripts.py`
- Verify: `tests/test_metrics.py`
- Verify: `tests/test_memory_quality_benchmark.py`

- [ ] **Step 1: Run targeted Python tests**

Run:

```bash
uv run pytest tests/test_scripts.py tests/test_metrics.py tests/test_memory_quality_benchmark.py -q
```

Expected: PASS.

- [ ] **Step 2: Run deterministic memory-quality benchmark**

Run:

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

Expected: exit code 0 and JSON summary with `"failed": 0`.

- [ ] **Step 3: Run provider benchmark help**

Run:

```bash
uv run python scripts/run_provider_experiments.py --help
```

Expected: exit code 0 and output contains `--provider {all,gpt,claude,deepseek}`.

- [ ] **Step 4: Run canonical fast gate**

Run:

```bash
./scripts/check.sh
```

Expected: exit code 0 with ruff success and pytest success.

- [ ] **Step 5: Inspect git status**

Run:

```bash
git status --short --branch
```

Expected: clean worktree on the implementation branch, ahead by the commits created in this plan.

## Self-Review Checklist

- Spec coverage:
  - Provider benchmark `--provider all|gpt|claude|deepseek` is covered by Tasks 1 and 2.
  - Provider benchmark public import compatibility is covered by Task 1.
  - Memory-quality strict scenario loading is covered by Task 3.
  - Trace-backed memory scoring is covered by Task 4.
  - Deterministic fake-mode runtime execution and JSON output are covered by Task 5.
  - Documentation drift cleanup is covered by Task 6.
  - Fast-gate verification is covered by Task 7.

- Type consistency:
  - Provider selector helper returns a tuple of strings.
  - Memory-quality row statuses are `"pass"` and `"fail"`.
  - Memory-quality JSON uses `schema_version`, `mode`, `summary`, and `rows`.
  - `main(argv=None)` returns integer exit codes for tests and script execution.

- Scope check:
  - The plan does not implement Phase 2 or Phase 3 packages.
  - Live provider memory-quality support is present through `--mode live --provider <provider>`, but no live provider call is required by the tests or fast gate.
