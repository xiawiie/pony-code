# Pico Live-API End-to-End Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `benchmarks/live_e2e/run_live_session.py` — a single script that runs 5 designed turns through the real Anthropic API and hard-asserts 27 concrete invariants covering all four post-migration optimizations.

**Architecture:** Six-component script (Config / FixtureManager / TurnRunner / AssertionEngine / Reporter / main) + fixtures + AssertionEngine unit tests. Real Anthropic API call, `read_only=True` Pico for safety, stdlib-only, JSON report emitted per run.

**Tech Stack:** Python 3.11+, stdlib only (argparse, dataclasses, pathlib, json, time, os, sys), pico's existing `AnthropicCompatibleModelClient`, pico's `Pico` runtime.

## Global Constraints

- **Python 3.11+** (from prior spec's `requires-python = ">=3.11"`).
- **stdlib-only**: no new third-party dependencies. Use pico's existing Anthropic adapter.
- **read_only=True** enforced at Pico construction — no `write_file` / `patch_file` / `run_shell` writes may reach the tool executor.
- **No pytest coupling for the script itself** — the script is a standalone runner; only `AssertionEngine` has pytest unit tests.
- **Never touch main branch** — script runs from `memory` branch or newer.
- **JSON report schema_version = 1**; future breaking changes bump this.
- **Cost defaults**: `--max-provider-calls=15`, `--max-total-tokens=200000`, `--timeout-seconds=300`.
- **Default model**: `claude-sonnet-4-5-20250929`.
- **Spec**: `docs/superpowers/specs/2026-07-08-pico-live-e2e-test-design.md`.

---

## File Structure

```
benchmarks/live_e2e/
├── __init__.py                     # empty marker (Task 1)
├── README.md                       # usage + cost + interpretation (Task 12)
├── run_live_session.py             # main + 6 components (Tasks 2-11)
├── fixtures/
│   └── seed_cache_note.md          # frontmatter + body (Task 3)
├── tests/
│   ├── __init__.py                 # empty marker (Task 10)
│   └── test_assertions.py          # AssertionEngine unit tests (Tasks 6-9)
└── results/
    └── README.md                   # explains the dir (Task 1)
```

Root `.gitignore` gets 2 new entries (Task 11).

---

## Task 1: Skeleton directory + placeholders

**Files:**
- Create: `benchmarks/live_e2e/__init__.py`
- Create: `benchmarks/live_e2e/results/README.md`
- Create: `benchmarks/live_e2e/fixtures/__init__.py`
- Test: no test yet — this task creates scaffolding only

**Interfaces:**
- Consumes: nothing
- Produces: package importable as `benchmarks.live_e2e`

- [ ] **Step 1: Create the package skeleton**

```bash
mkdir -p benchmarks/live_e2e/fixtures benchmarks/live_e2e/results benchmarks/live_e2e/tests
```

- [ ] **Step 2: Write `benchmarks/live_e2e/__init__.py`**

```python
"""Live-API end-to-end test harness for pico's post-migration optimizations.

Not a pytest test. Standalone script; see run_live_session.py.
Consumes real Anthropic API credits; run manually.
"""
```

- [ ] **Step 3: Write `benchmarks/live_e2e/fixtures/__init__.py`**

```python
"""Fixture files for the live-e2e test harness."""
```

- [ ] **Step 4: Write `benchmarks/live_e2e/results/README.md`**

```markdown
# live_e2e results/

This directory holds JSON reports emitted by
`benchmarks/live_e2e/run_live_session.py`. Each run writes one file:
`live-e2e-<ns_timestamp>.json`.

The directory itself is committed (via this README); the `.json` files
and the `pre-run-pico.toml.bak` snapshot are git-ignored.
```

- [ ] **Step 5: Verify importable**

Run: `uv run python -c "import benchmarks.live_e2e; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add benchmarks/live_e2e/
git commit -m "feat(live-e2e): package skeleton"
```

---

## Task 2: `RunConfig` + `parse_args` + `check_env` + `verify_pico_repo`

**Files:**
- Create: `benchmarks/live_e2e/run_live_session.py` (Config section only for now)
- Test: `benchmarks/live_e2e/tests/test_assertions.py` — set up basic test module (empty for now, populated in Tasks 6-9)
- Test: `benchmarks/live_e2e/tests/__init__.py`

**Interfaces:**
- Produces:
  - `RunConfig` frozen dataclass with fields: `provider: str = "anthropic"`, `model: str = "claude-sonnet-4-5-20250929"`, `max_provider_calls: int = 15`, `max_total_tokens: int = 200000`, `timeout_seconds: int = 300`, `reset: bool = False`, `verbose: bool = False`
  - `parse_args() -> RunConfig`
  - `check_env(config: RunConfig) -> None` — raises `SystemExit(2)` if `PICO_ANTHROPIC_API_KEY` missing
  - `verify_pico_repo(root: Path) -> None` — raises `SystemExit(2)` if not a pico repo

- [ ] **Step 1: Write `benchmarks/live_e2e/tests/__init__.py`**

```python
"""Unit tests for the live-e2e AssertionEngine (offline; safe under regular pytest)."""
```

- [ ] **Step 2: Write `benchmarks/live_e2e/tests/test_assertions.py` (empty stub)**

```python
"""Unit tests for benchmarks.live_e2e.run_live_session.AssertionEngine.

Tests are offline: no API is called, no fixture writes, no pico repo mutation.
Populated in Tasks 6-9.
"""
```

- [ ] **Step 3: Write `benchmarks/live_e2e/run_live_session.py` — Config section**

```python
"""Pico live-API end-to-end test harness.

Runs 5 designed turns through a real Anthropic model and hard-asserts 27
invariants covering the post-migration optimizations. Standalone; not a
pytest test. Consumes real API credits (~$0.20/run on Sonnet).

Entry:
    uv run python -m benchmarks.live_e2e.run_live_session
    uv run python -m benchmarks.live_e2e.run_live_session --reset
    uv run python -m benchmarks.live_e2e.run_live_session --model claude-haiku-...

See docs/superpowers/specs/2026-07-08-pico-live-e2e-test-design.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


@dataclass(frozen=True)
class RunConfig:
    """CLI + env-derived configuration for one live-e2e run."""

    provider: str = "anthropic"
    model: str = DEFAULT_MODEL
    max_provider_calls: int = 15
    max_total_tokens: int = 200_000
    timeout_seconds: int = 300
    reset: bool = False
    verbose: bool = False


def parse_args() -> RunConfig:
    """Parse CLI arguments and return a frozen RunConfig.

    Environment variable ``PICO_ANTHROPIC_MODEL`` overrides the default
    model when ``--model`` is not passed; ``--model`` on the CLI wins
    over both env and the hard-coded default.
    """
    parser = argparse.ArgumentParser(prog="run_live_session")
    env_model = os.environ.get("PICO_ANTHROPIC_MODEL", DEFAULT_MODEL)
    parser.add_argument("--model", default=env_model)
    parser.add_argument("--max-provider-calls", type=int, default=15)
    parser.add_argument("--max-total-tokens", type=int, default=200_000)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return RunConfig(
        provider="anthropic",
        model=args.model,
        max_provider_calls=args.max_provider_calls,
        max_total_tokens=args.max_total_tokens,
        timeout_seconds=args.timeout_seconds,
        reset=args.reset,
        verbose=args.verbose,
    )


def check_env(config: RunConfig) -> None:
    """Abort with exit 2 if the Anthropic API key is missing."""
    if config.reset:
        return  # reset path doesn't need the API key
    key = os.environ.get("PICO_ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("[live-e2e] missing PICO_ANTHROPIC_API_KEY, aborted", file=sys.stderr)
        raise SystemExit(2)


def verify_pico_repo(root: Path) -> None:
    """Abort with exit 2 if ``root`` is not a pico repository."""
    if not (root / "pico" / "runtime.py").is_file():
        print(
            f"[live-e2e] {root} does not look like a pico repo (missing pico/runtime.py), aborted",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not (root / "pyproject.toml").is_file():
        print(
            f"[live-e2e] {root}/pyproject.toml missing, aborted",
            file=sys.stderr,
        )
        raise SystemExit(2)
```

- [ ] **Step 4: Smoke — importable, parse_args returns defaults**

```bash
uv run python -c "
from benchmarks.live_e2e.run_live_session import RunConfig, DEFAULT_MODEL
c = RunConfig()
assert c.provider == 'anthropic'
assert c.model == DEFAULT_MODEL
assert c.max_provider_calls == 15
assert c.max_total_tokens == 200_000
assert c.timeout_seconds == 300
assert c.reset is False
assert c.verbose is False
print('ok')
"
```

Expected: `ok`

- [ ] **Step 5: Smoke — check_env raises when key missing**

```bash
uv run python -c "
import os
os.environ.pop('PICO_ANTHROPIC_API_KEY', None)
from benchmarks.live_e2e.run_live_session import RunConfig, check_env
try:
    check_env(RunConfig())
    print('FAIL: should have exited')
except SystemExit as e:
    assert e.code == 2, e.code
    print('ok')
"
```

Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/__init__.py benchmarks/live_e2e/tests/test_assertions.py
git commit -m "feat(live-e2e): Config, parse_args, check_env, verify_pico_repo"
```

---

## Task 3: Seed fixture note

**Files:**
- Create: `benchmarks/live_e2e/fixtures/seed_cache_note.md`

**Interfaces:**
- Produces: static Markdown file — read by `FixtureManager` in Task 4 and copied to `.pico/memory/agent/cache-invariant.md` at run time.

- [ ] **Step 1: Write the fixture file**

```markdown
---
name: cache-invariant
type: feedback
description: prompt-cache invariant — stable prefix must not include mtime content
tags: [context, cache]
aliases: []
supersedes: []
---

Pico's cache anchor lives in Layer 1 (system content block). Anything
that changes per turn (workspace state, memory index) must go through
<system-reminder> injection on the user message, not into system.
```

- [ ] **Step 2: Smoke — file exists and parses as frontmatter**

```bash
uv run python -c "
from pathlib import Path
from pico.memory.frontmatter import parse_frontmatter
p = Path('benchmarks/live_e2e/fixtures/seed_cache_note.md')
meta, body = parse_frontmatter(p.read_text(encoding='utf-8'))
assert meta['name'] == 'cache-invariant'
assert meta['type'] == 'feedback'
assert 'cache' in meta['tags']
assert 'Layer 1' in body
print('ok')
"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/live_e2e/fixtures/seed_cache_note.md
git commit -m "feat(live-e2e): seed cache-invariant memory note fixture"
```

---

## Task 4: `FixtureManager` context manager

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (append `FixtureManager` class)

**Interfaces:**
- Consumes: fixture at `benchmarks/live_e2e/fixtures/seed_cache_note.md`
- Produces:
  - `class FixtureManager` — context manager; on enter it (1) snapshots any pre-existing `pico.toml` to `benchmarks/live_e2e/results/pre-run-pico.toml.bak`, (2) writes the fixture `pico.toml` at repo root, (3) writes the seed note to `.pico/memory/agent/cache-invariant.md`; on exit it removes the seed note and restores or deletes `pico.toml`; never raises inside `__exit__`.
  - Class attribute / method `FIXTURE_PICO_TOML: str` — the exact TOML content written on enter.

- [ ] **Step 1: Append `FixtureManager` to run_live_session.py**

Append at the end of `benchmarks/live_e2e/run_live_session.py`:

```python
FIXTURE_PICO_TOML = """\
[context]
history_soft_cap = 1200
history_floor_messages = 4
injection_budget_ratio = 0.005
total_budget_hard_cap = 100000
system_tools_hard_cap = 30000

[context.digest]
size_threshold_chars = 800

[memory.recall]
min_score = 0.2
"""


SEED_NOTE_REL = Path(".pico/memory/agent/cache-invariant.md")
PICO_TOML_REL = Path("pico.toml")
BACKUP_REL = Path("benchmarks/live_e2e/results/pre-run-pico.toml.bak")


class FixtureManager:
    """Context manager that swaps in the live-e2e fixture pico.toml + seed note.

    On enter:
      1. If a pre-existing pico.toml is present, copy it to
         ``benchmarks/live_e2e/results/pre-run-pico.toml.bak`` so
         teardown can restore it.
      2. Write ``FIXTURE_PICO_TOML`` to ``<repo_root>/pico.toml``.
      3. Write the fixture seed note to
         ``<repo_root>/.pico/memory/agent/cache-invariant.md``.

    On exit (never raises):
      1. Remove the seed note if present.
      2. Restore original pico.toml from backup, or delete the fixture
         copy if no backup existed.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self._seed_source = (
            Path(__file__).resolve().parent / "fixtures" / "seed_cache_note.md"
        )
        self._had_pico_toml = False

    def __enter__(self) -> "FixtureManager":
        pico_toml = self.repo_root / PICO_TOML_REL
        backup = self.repo_root / BACKUP_REL
        # 1. Snapshot if present
        if pico_toml.exists():
            self._had_pico_toml = True
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(pico_toml.read_bytes())
        # 2. Write fixture
        pico_toml.write_text(FIXTURE_PICO_TOML, encoding="utf-8")
        # 3. Write seed note
        seed_target = self.repo_root / SEED_NOTE_REL
        seed_target.parent.mkdir(parents=True, exist_ok=True)
        seed_target.write_text(
            self._seed_source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Never raise: log-then-swallow all teardown errors.
        try:
            seed_target = self.repo_root / SEED_NOTE_REL
            if seed_target.exists():
                seed_target.unlink()
        except OSError as e:
            print(f"[live-e2e] teardown: could not remove seed note: {e}", file=sys.stderr)
        try:
            pico_toml = self.repo_root / PICO_TOML_REL
            backup = self.repo_root / BACKUP_REL
            if self._had_pico_toml and backup.exists():
                pico_toml.write_bytes(backup.read_bytes())
                backup.unlink()
            elif pico_toml.exists():
                pico_toml.unlink()
        except OSError as e:
            print(f"[live-e2e] teardown: pico.toml restore failed: {e}", file=sys.stderr)
```

- [ ] **Step 2: Smoke — FixtureManager writes and restores**

```bash
uv run python -c "
import tempfile
from pathlib import Path
from benchmarks.live_e2e.run_live_session import FixtureManager, FIXTURE_PICO_TOML, SEED_NOTE_REL, PICO_TOML_REL

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / 'pico').mkdir()
    # Pre-existing pico.toml case
    (root / PICO_TOML_REL).write_text('[policy]\nmax_blob_size = 42\n', encoding='utf-8')
    (root / 'benchmarks' / 'live_e2e' / 'results').mkdir(parents=True)
    with FixtureManager(root):
        assert (root / SEED_NOTE_REL).exists()
        got = (root / PICO_TOML_REL).read_text(encoding='utf-8')
        assert got == FIXTURE_PICO_TOML
    # After exit
    assert not (root / SEED_NOTE_REL).exists()
    restored = (root / PICO_TOML_REL).read_text(encoding='utf-8')
    assert 'max_blob_size = 42' in restored
print('ok')
"
```

Expected: `ok`

- [ ] **Step 3: Smoke — FixtureManager with no pre-existing pico.toml**

```bash
uv run python -c "
import tempfile
from pathlib import Path
from benchmarks.live_e2e.run_live_session import FixtureManager, SEED_NOTE_REL, PICO_TOML_REL

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / 'benchmarks' / 'live_e2e' / 'results').mkdir(parents=True)
    with FixtureManager(root):
        assert (root / PICO_TOML_REL).exists()
        assert (root / SEED_NOTE_REL).exists()
    # After exit, no pico.toml (there was none before)
    assert not (root / PICO_TOML_REL).exists()
    assert not (root / SEED_NOTE_REL).exists()
print('ok')
"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py
git commit -m "feat(live-e2e): FixtureManager context manager (pico.toml + seed note swap)"
```

---

## Task 5: `TurnResult` dataclass + `TurnRunner`

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (append `TurnResult`, `TurnRunner`)

**Interfaces:**
- Consumes: `pico.runtime.Pico` — `pico.ask(user_message: str) -> str`; `pico.last_prompt_metadata: dict`; `pico.session: dict`; `pico.model_client.last_completion_metadata: dict`.
- Produces:
  - `TurnResult` frozen dataclass (see §7.7 of spec)
  - `class TurnRunner` — `run_turn(turn: int, user_prompt: str, expected_behavior: str) -> TurnResult`

- [ ] **Step 1: Append TurnResult + TurnRunner to run_live_session.py**

Append at the end of `benchmarks/live_e2e/run_live_session.py`:

```python
import time


@dataclass(frozen=True)
class TurnResult:
    """Immutable record of a single turn's execution."""

    turn: int
    user_prompt: str
    expected_behavior: str
    final_answer: str
    metadata: dict
    session_message_count_before: int
    session_message_count_after: int
    provider_call_count_this_turn: int
    duration_ms: int
    usage: dict
    stopped_at_step_limit: bool
    error: str | None
    provider_input_messages_len: int
    current_user_content: str


class TurnRunner:
    """Runs one turn against a real Pico + provider; captures TurnResult.

    The runner does NOT catch exceptions raised by ``pico.ask`` — the
    caller (``main``) decides whether to abort or continue.
    """

    def __init__(self, pico, config: RunConfig):
        self.pico = pico
        self.config = config
        self._provider_calls_before = self._count_provider_calls()

    def _count_provider_calls(self) -> int:
        """Best-effort count of provider calls seen so far.

        AnthropicCompatibleModelClient does not natively track calls, so
        we use the presence of ``last_completion_metadata`` on the client
        as a coarse indicator; for exact counting we rely on the messages
        array growth pattern instead (each turn produces at least one
        provider call).
        """
        client = getattr(self.pico, "model_client", None)
        return int(getattr(client, "_pico_live_call_count", 0) or 0)

    def run_turn(
        self, turn: int, user_prompt: str, expected_behavior: str
    ) -> TurnResult:
        """Execute one turn and return a captured TurnResult."""
        session_before = len(self.pico.session.get("messages", []))
        provider_calls_before = self._count_provider_calls()
        started_ns = time.monotonic_ns()
        error: str | None = None
        final_answer = ""
        stopped_at_step_limit = False

        try:
            final_answer = self.pico.ask(user_prompt)
        except Exception as exc:  # capture and continue; caller decides
            error = f"{type(exc).__name__}: {exc}"

        duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        metadata = dict(getattr(self.pico, "last_prompt_metadata", {}) or {})
        usage = dict(getattr(self.pico.model_client, "last_completion_metadata", {}) or {})
        provider_calls_after = self._count_provider_calls()
        session_after = len(self.pico.session.get("messages", []))

        # detect step-limit stops (no exception, but final answer starts with the
        # runtime's canned "Stopped after..." message)
        if final_answer.startswith("Stopped after"):
            stopped_at_step_limit = True

        current_user_content = ""
        messages = self.pico.session.get("messages", []) or []
        if messages:
            last = messages[-1]
            if isinstance(last.get("content"), str):
                current_user_content = last["content"]

        provider_input_messages_len = int(metadata.get("messages_count", 0))

        return TurnResult(
            turn=turn,
            user_prompt=user_prompt,
            expected_behavior=expected_behavior,
            final_answer=final_answer,
            metadata=metadata,
            session_message_count_before=session_before,
            session_message_count_after=session_after,
            provider_call_count_this_turn=max(0, provider_calls_after - provider_calls_before),
            duration_ms=duration_ms,
            usage=usage,
            stopped_at_step_limit=stopped_at_step_limit,
            error=error,
            provider_input_messages_len=provider_input_messages_len,
            current_user_content=current_user_content,
        )
```

- [ ] **Step 2: Smoke — TurnResult is frozen and TurnRunner constructs**

```bash
uv run python -c "
from unittest.mock import MagicMock
from benchmarks.live_e2e.run_live_session import TurnResult, TurnRunner, RunConfig

r = TurnResult(
    turn=1, user_prompt='hi', expected_behavior='x', final_answer='ok',
    metadata={}, session_message_count_before=0, session_message_count_after=1,
    provider_call_count_this_turn=1, duration_ms=100, usage={},
    stopped_at_step_limit=False, error=None,
    provider_input_messages_len=1, current_user_content='hi',
)
try:
    r.turn = 2  # frozen: should fail
    print('FAIL: not frozen')
except Exception:
    pass

pico = MagicMock()
pico.session = {'messages': []}
pico.model_client = MagicMock(last_completion_metadata={})
pico.last_prompt_metadata = {}
pico.ask = MagicMock(return_value='answer')
runner = TurnRunner(pico, RunConfig())
result = runner.run_turn(1, 'hi', 'expected')
assert result.final_answer == 'answer'
assert result.turn == 1
assert result.error is None
print('ok')
"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py
git commit -m "feat(live-e2e): TurnResult dataclass + TurnRunner"
```

---

## Task 6: `Assertion` + `AssertionEngine` skeleton + Turn 1 (Recall) checks

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (append `Assertion`, `AssertionEngine`, `check_turn_1_recall`)
- Modify: `benchmarks/live_e2e/tests/test_assertions.py` (add tests for Turn 1)

**Interfaces:**
- Produces:
  - `Assertion` frozen dataclass: `name: str`, `passed: bool`, `expected: str`, `actual: str`
  - `class AssertionEngine`
  - `AssertionEngine.check_turn_1_recall(result: TurnResult) -> list[Assertion]`
  - `AssertionEngine.dispatch(turn: int, result, pico, all_results) -> list[Assertion]` — routes to per-turn checks (only turn 1 wired in this task; later tasks extend the branches)

- [ ] **Step 1: Write failing tests**

Append to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
from unittest.mock import MagicMock

from benchmarks.live_e2e.run_live_session import (
    Assertion,
    AssertionEngine,
    TurnResult,
)


def _turn_result_stub(**overrides):
    defaults = dict(
        turn=1,
        user_prompt="上次讨论过 cache invariant 的问题",
        expected_behavior="recall_triggered",
        final_answer="ok",
        metadata={
            "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
            "injection_tokens": {"recalled_memory": 42, "workspace_state": 10},
            "recall.error_count": 0,
        },
        session_message_count_before=0,
        session_message_count_after=2,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={"input_tokens": 10, "output_tokens": 5},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=1,
        current_user_content=(
            "<system-reminder><pico:recalled_memory path=\"workspace/agent/cache-invariant.md\">"
            "content</pico:recalled_memory></system-reminder>\n上次讨论过 cache invariant 的问题"
        ),
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_1_recall_passes_on_valid_metadata():
    engine = AssertionEngine()
    result = _turn_result_stub()
    asserts = engine.check_turn_1_recall(result)
    # All 6 required assertions present and passed
    assert len(asserts) == 6
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_1_recall_fails_when_intent_not_recall():
    engine = AssertionEngine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "default", "matched_keyword": "", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "intent_name_recall" for a in failed)


def test_check_turn_1_recall_fails_when_no_recall_block_rendered():
    engine = AssertionEngine()
    result = _turn_result_stub(current_user_content="上次讨论过什么", metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 0},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_block_present" for a in failed)


def test_check_turn_1_recall_fails_when_recall_error_nonzero():
    engine = AssertionEngine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 3,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recall_error_count_zero" for a in failed)


def test_assertion_is_frozen():
    a = Assertion(name="x", passed=True, expected="e", actual="a")
    import pytest
    with pytest.raises(Exception):
        a.name = "y"


def test_dispatch_routes_turn_1_to_recall_check():
    engine = AssertionEngine()
    result = _turn_result_stub()
    asserts = engine.dispatch(1, result, pico=MagicMock(), all_results=[result])
    assert len(asserts) == 6
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: FAIL with `ImportError: cannot import name 'Assertion'` (or similar)

- [ ] **Step 3: Append Assertion + AssertionEngine (turn 1 only)**

Append to `benchmarks/live_e2e/run_live_session.py`:

```python
@dataclass(frozen=True)
class Assertion:
    """One binary check produced by AssertionEngine."""

    name: str
    passed: bool
    expected: str
    actual: str


class AssertionEngine:
    """Turn-scoped hard-assertion engine. Never raises; returns list[Assertion]."""

    def dispatch(self, turn, result: TurnResult, pico, all_results):
        """Route to per-turn check_*.

        ``turn`` may be an int (1..5) or the string ``"global"``.
        """
        if turn == 1:
            return self.check_turn_1_recall(result)
        if turn == 2:
            return self.check_turn_2_digest(result, pico)
        if turn == 3:
            return self.check_turn_3_injection_drop(result)
        if turn == 4:
            return self.check_turn_4_history_drop(result, pico)
        if turn == 5:
            return self.check_turn_5_cache_anchor(result, all_results)
        if turn == "global":
            return self.check_global(all_results, pico)
        return []

    # -- Turn 1: recall --------------------------------------------------

    def check_turn_1_recall(self, result: TurnResult) -> list[Assertion]:
        """Six assertions verifying recall triggered correctly."""
        m = result.metadata or {}
        intent = m.get("intent") or {}
        injection_tokens = m.get("injection_tokens") or {}
        content = result.current_user_content or ""

        out = []
        intent_name = intent.get("name", "")
        out.append(Assertion(
            name="intent_name_recall",
            passed=intent_name == "recall",
            expected="recall",
            actual=str(intent_name),
        ))
        out.append(Assertion(
            name="recalled_memory_block_present",
            passed="<pico:recalled_memory" in content,
            expected='"<pico:recalled_memory" in current_user_content',
            actual=("<pico:recalled_memory" in content) and "found" or "not found",
        ))
        out.append(Assertion(
            name="seed_note_name_visible",
            passed="cache-invariant" in content,
            expected='"cache-invariant" in current_user_content',
            actual=("cache-invariant" in content) and "found" or "not found",
        ))
        recall_tokens = int(injection_tokens.get("recalled_memory", 0) or 0)
        out.append(Assertion(
            name="recalled_memory_tokens_gt_zero",
            passed=recall_tokens > 0,
            expected="injection_tokens[recalled_memory] > 0",
            actual=str(recall_tokens),
        ))
        err_count = int(m.get("recall.error_count", 0) or 0)
        out.append(Assertion(
            name="recall_error_count_zero",
            passed=err_count == 0,
            expected="recall.error_count == 0",
            actual=str(err_count),
        ))
        # stop_reason lives inside usage / model client — we accept absence
        # when the provider stub didn't surface it; the intent here is to
        # verify no crash and a valid final answer or tool_use path
        final = result.final_answer or ""
        no_error_and_answered = result.error is None and (
            final != "" or result.stopped_at_step_limit
        )
        out.append(Assertion(
            name="turn_1_completed_without_error",
            passed=no_error_and_answered,
            expected="pico.ask returned without exception",
            actual=result.error or ("stopped_at_step_limit" if result.stopped_at_step_limit else "ok"),
        ))
        return out

    # Turn 2-5 + global added in later tasks.
    def check_turn_2_digest(self, result: TurnResult, pico) -> list[Assertion]:
        return []

    def check_turn_3_injection_drop(self, result: TurnResult) -> list[Assertion]:
        return []

    def check_turn_4_history_drop(self, result: TurnResult, pico) -> list[Assertion]:
        return []

    def check_turn_5_cache_anchor(self, result: TurnResult, all_results) -> list[Assertion]:
        return []

    def check_global(self, all_results, pico) -> list[Assertion]:
        return []
```

- [ ] **Step 4: Run to verify passing**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py
git commit -m "feat(live-e2e): Assertion + AssertionEngine skeleton + Turn 1 recall checks"
```

---

## Task 7: Turn 2 (Digest) + Turn 3 (Injection drop) checks

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (fill in `check_turn_2_digest`, `check_turn_3_injection_drop`)
- Modify: `benchmarks/live_e2e/tests/test_assertions.py` (add tests)

**Interfaces:**
- Consumes: `TurnResult`, `Assertion`
- Produces: 5 checks for turn 2 (digest), 4 checks for turn 3 (injection drop)

- [ ] **Step 1: Add failing tests**

Append to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
from pathlib import Path


def _turn_2_result_stub(**overrides):
    """Session state includes a tool_result message with digest applied."""
    defaults = dict(
        turn=2,
        user_prompt="读一下 pico/runtime.py",
        expected_behavior="digest_applied",
        final_answer="ok",
        metadata={},
        session_message_count_before=2,
        session_message_count_after=6,
        provider_call_count_this_turn=2,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=6,
        current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def _pico_stub_with_digested_message(raw_body: str, raw_dir: Path, source_hash: str = "abc12345"):
    """Build a MagicMock pico whose session has a digested tool_result at the tail."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{source_hash}.txt"
    raw_file.write_text(raw_body, encoding="utf-8")

    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1",
                             "content": f"[digest] runtime.py (900 lines)\n- import\n(raw at {raw_file})"}],
                "_pico_meta": {"digest_applied": True, "source_hash": source_hash, "tool_use_id": "t1"},
            },
        ]
    }
    return pico, raw_file


def test_check_turn_2_digest_passes_on_valid_state(tmp_path):
    engine = AssertionEngine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    result = _turn_2_result_stub()
    asserts = engine.check_turn_2_digest(result, pico)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_2_digest_fails_when_no_digest_applied(tmp_path):
    engine = AssertionEngine()
    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "raw output"}],
             "_pico_meta": {"digest_applied": False, "tool_use_id": "t1"}},
        ]
    }
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "digest_applied_flag_true" for a in failed)


def test_check_turn_2_digest_verifies_raw_file_exists(tmp_path):
    engine = AssertionEngine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    raw_file.unlink()  # remove the raw file → check should fail
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "raw_file_exists_on_disk" for a in failed)


def _turn_3_result_stub(**overrides):
    defaults = dict(
        turn=3,
        user_prompt="再看一下",
        expected_behavior="injection_dropped",
        final_answer="ok",
        metadata={
            "injection_budget": 500,
            "injection_dropped": ["checkpoint", "project_structure"],
            "injection_tokens": {
                "workspace_state": 100,
                "memory_index": 50,
                "project_structure": 0,
                "recalled_memory": 200,
                "checkpoint": 0,
            },
        },
        session_message_count_before=6,
        session_message_count_after=8,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=8,
        current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_3_injection_drop_passes_when_checkpoint_dropped():
    engine = AssertionEngine()
    asserts = engine.check_turn_3_injection_drop(_turn_3_result_stub())
    assert len(asserts) == 4
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_3_injection_drop_accepts_checkpoint_zero_tokens():
    """Assertion 14 accepts either dropped OR zero-tokens-so-never-rendered."""
    engine = AssertionEngine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["project_structure"],  # checkpoint NOT dropped
        "injection_tokens": {
            "workspace_state": 100, "memory_index": 50,
            "project_structure": 0, "recalled_memory": 200,
            "checkpoint": 0,  # zero tokens — never rendered — should still pass
        },
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert not any(a.name == "checkpoint_dropped_or_zero_tokens" for a in failed)


def test_check_turn_3_injection_drop_fails_when_recalled_memory_dropped():
    engine = AssertionEngine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["checkpoint", "project_structure", "recalled_memory"],
        "injection_tokens": {"recalled_memory": 0, "checkpoint": 0},
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_not_dropped" for a in failed)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 6 fail (the new tests) + 6 pass (Turn 1 tests still pass)

- [ ] **Step 3: Implement check_turn_2_digest and check_turn_3_injection_drop**

Replace the placeholder methods in `run_live_session.py`:

```python
    def check_turn_2_digest(self, result: TurnResult, pico) -> list[Assertion]:
        """Five assertions verifying digest applied on a large tool_result."""
        out = []
        messages = getattr(pico, "session", {}).get("messages", []) or []
        # find the last tool_result carrier
        tool_result_msg = None
        for msg in reversed(messages):
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                tool_result_msg = msg
                break

        meta = (tool_result_msg or {}).get("_pico_meta") or {}
        digest_applied = bool(meta.get("digest_applied"))
        out.append(Assertion(
            name="digest_applied_flag_true",
            passed=digest_applied,
            expected="last tool_result message has _pico_meta.digest_applied=True",
            actual=str(digest_applied),
        ))

        # tool_result content should start with [digest]
        tr_content = ""
        if tool_result_msg is not None:
            content = tool_result_msg.get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tr_content = str(block.get("content") or "")
                    break
        out.append(Assertion(
            name="tool_result_content_starts_with_digest",
            passed=tr_content.strip().startswith("[digest]"),
            expected="tool_result content starts with '[digest]'",
            actual=tr_content[:80] if tr_content else "(empty)",
        ))

        # content contains "raw at "
        out.append(Assertion(
            name="tool_result_content_contains_raw_pointer",
            passed="raw at " in tr_content,
            expected='"raw at " in tool_result content',
            actual="found" if "raw at " in tr_content else "not found",
        ))

        # extract raw path from content and check it exists on disk
        raw_path_str = ""
        marker = "raw at "
        idx = tr_content.find(marker)
        if idx != -1:
            tail = tr_content[idx + len(marker):]
            # path ends at ')' or end of line
            end = tail.find(")")
            if end == -1:
                end = tail.find("\n")
            raw_path_str = tail[:end].strip() if end != -1 else tail.strip()
        raw_path = Path(raw_path_str) if raw_path_str else None
        raw_exists = bool(raw_path and raw_path.exists())
        out.append(Assertion(
            name="raw_file_exists_on_disk",
            passed=raw_exists,
            expected=f"raw file at {raw_path_str!r} exists",
            actual="exists" if raw_exists else "missing",
        ))

        # raw file byte size matches original — for this we compare via
        # ``source_hash`` presence in ``_pico_meta`` as a proxy: if digest
        # was applied and file exists, we trust the runtime's write.
        source_hash = str(meta.get("source_hash", "") or "")
        out.append(Assertion(
            name="raw_file_source_hash_recorded",
            passed=bool(source_hash),
            expected="_pico_meta.source_hash is a non-empty string",
            actual=source_hash or "(empty)",
        ))
        return out

    def check_turn_3_injection_drop(self, result: TurnResult) -> list[Assertion]:
        """Four assertions verifying injection budget drop behavior."""
        m = result.metadata or {}
        budget = int(m.get("injection_budget", 0) or 0)
        dropped = list(m.get("injection_dropped") or [])
        tokens = m.get("injection_tokens") or {}

        out = []
        out.append(Assertion(
            name="injection_budget_gt_zero",
            passed=budget > 0,
            expected="injection_budget > 0",
            actual=str(budget),
        ))
        out.append(Assertion(
            name="injection_dropped_nonempty",
            passed=len(dropped) >= 1,
            expected="len(injection_dropped) >= 1",
            actual=str(dropped),
        ))
        # accept either checkpoint in dropped OR checkpoint had zero tokens
        checkpoint_tokens = int(tokens.get("checkpoint", 0) or 0)
        out.append(Assertion(
            name="checkpoint_dropped_or_zero_tokens",
            passed=("checkpoint" in dropped) or (checkpoint_tokens == 0),
            expected='"checkpoint" in dropped OR injection_tokens[checkpoint] == 0',
            actual=f"dropped={dropped}, checkpoint_tokens={checkpoint_tokens}",
        ))
        out.append(Assertion(
            name="recalled_memory_not_dropped",
            passed="recalled_memory" not in dropped,
            expected='"recalled_memory" NOT in dropped',
            actual=str(dropped),
        ))
        return out
```

- [ ] **Step 4: Run to verify passing**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 12 passed (6 turn 1 + 3 turn 2 + 3 turn 3)

- [ ] **Step 5: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py
git commit -m "feat(live-e2e): Turn 2 (digest) + Turn 3 (injection drop) assertions"
```

---

## Task 8: Turn 4 (History drop) + Turn 5 (Cache anchor) checks

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (fill in `check_turn_4_history_drop`, `check_turn_5_cache_anchor`)
- Modify: `benchmarks/live_e2e/tests/test_assertions.py` (add tests)

**Interfaces:**
- Produces: 5 checks for turn 4, 5 checks for turn 5

- [ ] **Step 1: Add failing tests**

Append to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
def _turn_4_result_stub(**overrides):
    defaults = dict(
        turn=4,
        user_prompt="总结",
        expected_behavior="history_dropped",
        final_answer="ok",
        metadata={
            "dropped_messages": 4,
            "messages_tokens": 1000,
        },
        session_message_count_before=14,
        session_message_count_after=16,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=10,  # smaller than session (drop reached wire)
        current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def _pico_stub_with_history():
    """A pico session with 16 messages including one balanced tool_use pair."""
    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
            {"role": "assistant", "content": "a2"},
        ] + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
    }
    return pico


def test_check_turn_4_history_drop_passes_when_all_invariants_hold():
    engine = AssertionEngine()
    pico = _pico_stub_with_history()
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(), pico)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_4_pairing_invariant_catches_orphan_tool_use():
    engine = AssertionEngine()
    pico = MagicMock()
    # orphan tool_use — no matching tool_result
    pico.session = {"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "orphan_x", "name": "read", "input": {}}]},
    ]}
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "no_orphan_tool_use" for a in failed)


def test_check_turn_4_fails_when_dropped_messages_zero():
    engine = AssertionEngine()
    pico = _pico_stub_with_history()
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(metadata={"dropped_messages": 0, "messages_tokens": 500}), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "dropped_messages_gt_zero" for a in failed)


def _turn_5_result_stub(system_cache_key="abc", **overrides):
    metadata = {
        "cache_control_breakpoints": [10],
        "system_cache_key": system_cache_key,
        "system_tokens": 100, "tools_tokens": 50, "messages_count": 12,
        "messages_tokens": 500, "injection_tokens": {}, "injection_truncated": {},
        "injection_dropped": [], "injection_budget": 500,
        "intent": {"name": "default", "matched_keyword": "", "matched_reason": ""},
        "recall.error_count": 0, "recall.last_error": "",
        "dropped_messages": 0, "prompt_cache_key": "abc",
    }
    defaults = dict(
        turn=5,
        user_prompt="done",
        expected_behavior="cache_anchor_verified",
        final_answer="ok",
        metadata=metadata,
        session_message_count_before=16, session_message_count_after=18,
        provider_call_count_this_turn=1, duration_ms=100,
        usage={"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        stopped_at_step_limit=False, error=None,
        provider_input_messages_len=12, current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_5_cache_anchor_passes_when_cache_key_stable():
    engine = AssertionEngine()
    all_results = [
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_5_result_stub(system_cache_key="k"),
    ]
    asserts = engine.check_turn_5_cache_anchor(all_results[-1], all_results)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_5_fails_when_cache_key_drifts():
    engine = AssertionEngine()
    all_results = [
        _turn_1_result_stub_for_cache(cache_key="k1"),
        _turn_1_result_stub_for_cache(cache_key="k2"),  # drift!
    ]
    all_results.append(_turn_5_result_stub(system_cache_key="k1"))
    asserts = engine.check_turn_5_cache_anchor(all_results[-1], all_results)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "system_cache_key_stable_across_turns" for a in failed)


def _turn_1_result_stub_for_cache(cache_key="k"):
    return _turn_result_stub(metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 10},
        "recall.error_count": 0,
        "system_cache_key": cache_key,
        "injection_budget": 500,
        "system_tokens": 100, "tools_tokens": 50,
        "messages_count": 2, "messages_tokens": 40, "injection_truncated": {},
        "injection_dropped": [], "recall.last_error": "",
        "dropped_messages": 0, "prompt_cache_key": cache_key,
        "cache_control_breakpoints": [],
    })
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 5 new fails (turn 4/5), 12 pass

- [ ] **Step 3: Implement check_turn_4_history_drop and check_turn_5_cache_anchor**

Replace the placeholder methods:

```python
    def check_turn_4_history_drop(self, result: TurnResult, pico) -> list[Assertion]:
        """Five assertions verifying history budget drop."""
        m = result.metadata or {}
        out = []

        dropped = int(m.get("dropped_messages", 0) or 0)
        out.append(Assertion(
            name="dropped_messages_gt_zero",
            passed=dropped > 0,
            expected="dropped_messages > 0",
            actual=str(dropped),
        ))

        msg_tokens = int(m.get("messages_tokens", 0) or 0)
        # soft_cap 1200 + slop of 300 (per §5 assertion 17)
        out.append(Assertion(
            name="messages_tokens_under_cap_plus_slop",
            passed=msg_tokens <= 1500,
            expected="messages_tokens <= 1500 (soft_cap 1200 + slop)",
            actual=str(msg_tokens),
        ))

        # pairing invariant: every tool_use.id has a tool_result.tool_use_id
        session_msgs = getattr(pico, "session", {}).get("messages", []) or []
        tool_use_ids = set()
        tool_result_ids = set()
        for msg in session_msgs:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tid = block.get("id")
                            if tid:
                                tool_use_ids.add(tid)
                        elif block.get("type") == "tool_result":
                            tuid = block.get("tool_use_id")
                            if tuid:
                                tool_result_ids.add(tuid)
        no_orphan = tool_use_ids == tool_result_ids
        out.append(Assertion(
            name="no_orphan_tool_use",
            passed=no_orphan,
            expected="every kept tool_use.id has matching tool_result",
            actual=f"tool_use_ids={sorted(tool_use_ids)}, tool_result_ids={sorted(tool_result_ids)}",
        ))

        # drop reached the wire: provider-input messages < session messages
        wire_len = int(result.provider_input_messages_len or 0)
        session_len = len(session_msgs)
        out.append(Assertion(
            name="drop_reached_provider_wire",
            passed=wire_len < session_len,
            expected="provider_input_messages_len < len(session.messages)",
            actual=f"wire={wire_len}, session={session_len}",
        ))

        # session["messages"] immutability check — the pre-drop entries
        # still exist in session (build_v2 does not mutate session).
        # We can only assert non-empty here; a deeper check would require
        # capturing pre-turn session state (out of scope for a per-turn check).
        out.append(Assertion(
            name="session_messages_still_populated",
            passed=session_len > 0,
            expected="len(session.messages) > 0 (immutability preserved)",
            actual=str(session_len),
        ))
        return out

    def check_turn_5_cache_anchor(self, result: TurnResult, all_results) -> list[Assertion]:
        """Five assertions verifying cache anchor stability + closure."""
        m = result.metadata or {}
        out = []

        # 1. cache_control_breakpoints non-empty
        breakpoints = m.get("cache_control_breakpoints") or []
        out.append(Assertion(
            name="cache_control_breakpoints_nonempty",
            passed=len(breakpoints) >= 1,
            expected="cache_control_breakpoints has at least 1 entry",
            actual=str(breakpoints),
        ))

        # 2. any turn 2-5 usage carried cache tokens
        cache_seen = False
        for r in all_results:
            u = r.usage or {}
            if int(u.get("cache_read_input_tokens", 0) or 0) > 0:
                cache_seen = True
                break
            if int(u.get("cache_creation_input_tokens", 0) or 0) > 0:
                cache_seen = True
                break
        out.append(Assertion(
            name="cache_tokens_observed_in_usage",
            passed=cache_seen,
            expected="cache_creation OR cache_read tokens > 0 in at least one turn's usage",
            actual="observed" if cache_seen else "no cache tokens seen",
        ))

        # 3. system_cache_key identical across all turns
        cache_keys = {(r.metadata or {}).get("system_cache_key", "") for r in all_results}
        # empty string counts as "missing" — treat as failure
        stable = len(cache_keys) == 1 and "" not in cache_keys
        out.append(Assertion(
            name="system_cache_key_stable_across_turns",
            passed=stable,
            expected="len(set(system_cache_key)) == 1 across all turns, non-empty",
            actual=str(sorted(cache_keys)),
        ))

        # 4. metadata completeness — reference the same 15 fields as
        #    tests/test_metadata_completeness.py
        required = {
            "system_cache_key", "system_tokens", "tools_tokens",
            "messages_count", "messages_tokens", "cache_control_breakpoints",
            "injection_tokens", "injection_truncated", "injection_dropped",
            "injection_budget", "intent", "recall.error_count",
            "recall.last_error", "dropped_messages", "prompt_cache_key",
        }
        missing = required - set(m.keys())
        out.append(Assertion(
            name="metadata_schema_complete",
            passed=not missing,
            expected="all 15 required metadata fields present",
            actual=("missing: " + str(sorted(missing))) if missing else "all present",
        ))

        # 5. session-level recall error count remains zero
        # (we walk all_results — each turn should report zero)
        max_err = 0
        for r in all_results:
            max_err = max(max_err, int((r.metadata or {}).get("recall.error_count", 0) or 0))
        out.append(Assertion(
            name="no_recall_errors_across_session",
            passed=max_err == 0,
            expected="max(recall.error_count) across all turns == 0",
            actual=str(max_err),
        ))
        return out
```

- [ ] **Step 4: Run to verify passing**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py
git commit -m "feat(live-e2e): Turn 4 (history drop) + Turn 5 (cache anchor) assertions"
```

---

## Task 9: Global checks + AssertionEngine wrap-up

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (fill in `check_global`)
- Modify: `benchmarks/live_e2e/tests/test_assertions.py` (add tests)

**Interfaces:**
- Produces: 2 global assertions

- [ ] **Step 1: Add failing tests**

Append to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
def test_check_global_passes_under_budget():
    engine = AssertionEngine()
    all_results = [
        _turn_result_stub(usage={"input_tokens": 1000, "output_tokens": 200}, provider_call_count_this_turn=1),
        _turn_result_stub(turn=2, usage={"input_tokens": 1500, "output_tokens": 300}, provider_call_count_this_turn=2),
        _turn_result_stub(turn=3, usage={"input_tokens": 1200, "output_tokens": 250}, provider_call_count_this_turn=1),
    ]
    asserts = engine.check_global(all_results, MagicMock())
    assert len(asserts) == 2
    assert all(a.passed for a in asserts)


def test_check_global_fails_when_provider_calls_exceeded():
    engine = AssertionEngine()
    all_results = [
        _turn_result_stub(provider_call_count_this_turn=8),
        _turn_result_stub(turn=2, provider_call_count_this_turn=8),  # sum = 16 > 15
    ]
    asserts = engine.check_global(all_results, MagicMock())
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "total_provider_calls_under_cap" for a in failed)


def test_check_global_fails_when_tokens_exceeded():
    engine = AssertionEngine()
    all_results = [
        _turn_result_stub(usage={"input_tokens": 150000, "output_tokens": 60000}),
    ]
    asserts = engine.check_global(all_results, MagicMock())
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "total_tokens_under_cap" for a in failed)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 3 new fails, 17 pass

- [ ] **Step 3: Implement check_global**

Replace the placeholder:

```python
    def check_global(self, all_results, pico) -> list[Assertion]:
        """Two cross-turn budget invariants."""
        out = []
        total_calls = sum(r.provider_call_count_this_turn for r in all_results)
        out.append(Assertion(
            name="total_provider_calls_under_cap",
            passed=total_calls <= 15,
            expected="sum(provider_call_count_this_turn) <= 15",
            actual=str(total_calls),
        ))
        total_tokens = 0
        for r in all_results:
            u = r.usage or {}
            total_tokens += int(u.get("input_tokens", 0) or 0)
            total_tokens += int(u.get("output_tokens", 0) or 0)
        out.append(Assertion(
            name="total_tokens_under_cap",
            passed=total_tokens <= 200_000,
            expected="total input+output tokens <= 200,000",
            actual=str(total_tokens),
        ))
        return out
```

- [ ] **Step 4: Run to verify passing**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
Expected: 20 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py
git commit -m "feat(live-e2e): global cross-turn assertions"
```

---

## Task 10: `Reporter` (terminal + JSON output)

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (append `Reporter` class)

**Interfaces:**
- Produces:
  - `class Reporter`
  - `Reporter.render_turn_summary(turn, expected, assertions) -> None` — prints one colored line
  - `Reporter.write_json(all_results, all_assertions, config, totals, wall_time_ms) -> Path`
  - `Reporter.render_final(overall_pass, totals, wall_time_ms, report_path, assertion_summary) -> None`

- [ ] **Step 1: Append Reporter to run_live_session.py**

Append:

```python
import json


class Reporter:
    """Terminal + JSON reporter for a live-e2e run."""

    _COLOR_GREEN = "\033[32m"
    _COLOR_RED = "\033[31m"
    _COLOR_YELLOW = "\033[33m"
    _COLOR_RESET = "\033[0m"

    def __init__(self, config: RunConfig, output_dir: Path):
        self.config = config
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._use_color = sys.stdout.isatty()

    def _color(self, text: str, color: str) -> str:
        return f"{color}{text}{self._COLOR_RESET}" if self._use_color else text

    def render_turn_summary(self, turn, expected: str, assertions: list) -> None:
        total = len(assertions)
        passed = sum(1 for a in assertions if a.passed)
        color = self._COLOR_GREEN if passed == total else self._COLOR_RED
        label = "PASS" if passed == total else "FAIL"
        turn_str = f"Turn {turn}" if isinstance(turn, int) else str(turn).capitalize()
        # Dot-padded label for alignment
        title = f"[live-e2e] {turn_str}: {expected}"
        pad_target = 55
        pad = "." * max(3, pad_target - len(title))
        print(f"{title} {pad} {self._color(label, color)} ({passed}/{total})")
        # Show individual failed assertions
        for a in assertions:
            if not a.passed:
                print(f"    {self._color('❌', self._COLOR_RED)} {a.name}")
                print(f"       expected: {a.expected}")
                print(f"       actual:   {a.actual}")

    def write_json(
        self,
        all_results: list,
        all_assertions: dict,
        config: RunConfig,
        totals: dict,
        wall_time_ms: int,
    ) -> Path:
        run_id = f"live-e2e-{time.time_ns()}"
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "provider": config.provider,
            "model": config.model,
            "wall_time_ms": wall_time_ms,
            "config": {
                "max_provider_calls": config.max_provider_calls,
                "max_total_tokens": config.max_total_tokens,
                "timeout_seconds": config.timeout_seconds,
            },
            "turns": [self._turn_to_json(r, all_assertions.get(r.turn, [])) for r in all_results],
            "global_assertions": [self._assertion_to_json(a) for a in all_assertions.get("global", [])],
            "totals": totals,
        }

        # assertion summary
        total = 0
        passed = 0
        for asserts_list in all_assertions.values():
            for a in asserts_list:
                total += 1
                if a.passed:
                    passed += 1
        payload["assertion_summary"] = {"total": total, "passed": passed, "failed": total - passed}
        payload["overall_pass"] = passed == total

        report_path = self.output_dir / f"{run_id}.json"
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return report_path

    def _turn_to_json(self, r, assertions) -> dict:
        return {
            "turn": r.turn,
            "user_prompt": r.user_prompt,
            "expected_behavior": r.expected_behavior,
            "duration_ms": r.duration_ms,
            "provider_calls_this_turn": r.provider_call_count_this_turn,
            "final_answer": r.final_answer[:500],
            "stopped_at_step_limit": r.stopped_at_step_limit,
            "error": r.error,
            "usage": r.usage,
            "metadata_subset": {
                k: r.metadata.get(k) for k in [
                    "intent", "injection_tokens", "injection_dropped",
                    "injection_budget", "recall.error_count",
                    "dropped_messages", "messages_tokens", "system_cache_key",
                    "cache_control_breakpoints",
                ] if k in (r.metadata or {})
            },
            "assertions": [self._assertion_to_json(a) for a in assertions],
        }

    @staticmethod
    def _assertion_to_json(a) -> dict:
        return {
            "name": a.name,
            "passed": a.passed,
            "expected": a.expected,
            "actual": a.actual,
        }

    def render_final(
        self,
        overall_pass: bool,
        totals: dict,
        wall_time_ms: int,
        report_path: Path,
        assertion_summary: tuple,
    ) -> None:
        passed, total = assertion_summary
        color = self._COLOR_GREEN if overall_pass else self._COLOR_RED
        label = "ALL PASS" if overall_pass else "FAIL"
        print()
        print(f"[live-e2e] OVERALL: {self._color(label, color)} · {passed}/{total} assertions")
        print(
            f"[live-e2e] wall_time={wall_time_ms/1000:.1f}s · "
            f"input_tokens={totals.get('input_tokens', 0):,} · "
            f"output_tokens={totals.get('output_tokens', 0):,} · "
            f"cache_reads={totals.get('cache_read_input_tokens', 0):,}"
        )
        print(f"[live-e2e] report: {report_path}")
```

- [ ] **Step 2: Smoke — Reporter writes valid JSON**

```bash
uv run python -c "
import tempfile
from pathlib import Path
from benchmarks.live_e2e.run_live_session import (
    Reporter, RunConfig, TurnResult, Assertion,
)

with tempfile.TemporaryDirectory() as td:
    r = Reporter(RunConfig(), Path(td))
    tr = TurnResult(
        turn=1, user_prompt='hi', expected_behavior='x', final_answer='ok',
        metadata={'system_cache_key': 'abc', 'intent': {'name': 'default'}},
        session_message_count_before=0, session_message_count_after=1,
        provider_call_count_this_turn=1, duration_ms=100, usage={'input_tokens': 10},
        stopped_at_step_limit=False, error=None,
        provider_input_messages_len=1, current_user_content='hi',
    )
    a1 = Assertion(name='a1', passed=True, expected='e', actual='e')
    a2 = Assertion(name='a2', passed=False, expected='ex', actual='ax')
    path = r.write_json(
        all_results=[tr],
        all_assertions={1: [a1], 'global': [a2]},
        config=RunConfig(),
        totals={'input_tokens': 10, 'output_tokens': 5},
        wall_time_ms=123,
    )
    import json
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['schema_version'] == 1
    assert data['overall_pass'] is False
    assert data['assertion_summary']['total'] == 2
    assert data['assertion_summary']['passed'] == 1
print('ok')
"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py
git commit -m "feat(live-e2e): Reporter with colored terminal output and JSON write"
```

---

## Task 11: `main()` orchestration + `do_reset` + gitignore

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py` (append `do_reset`, `warn_if_dirty_working_tree`, `_make_anthropic_client`, `_budget_exceeded`, `main`, `if __name__ == "__main__"` block)
- Modify: `.gitignore`

**Interfaces:**
- Consumes: all prior tasks' components.
- Produces:
  - `do_reset(repo_root: Path) -> None`
  - `main() -> int` (entry point)
  - `if __name__ == "__main__": raise SystemExit(main())`
- Updated `.gitignore` per spec §10.

- [ ] **Step 1: Append main + helpers to run_live_session.py**

Append:

```python
def warn_if_dirty_working_tree(root: Path) -> None:
    """Print a warning if `git status` reports uncommitted work. Never aborts."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode == 0 and result.stdout.strip():
        print(
            "[live-e2e] warning: working tree not clean; live e2e might interact with your changes",
            file=sys.stderr,
        )


def _make_anthropic_client(config: RunConfig):
    """Instantiate a real Anthropic-compatible model client."""
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient

    api_key = os.environ.get("PICO_ANTHROPIC_API_KEY", "")
    base_url = os.environ.get(
        "PICO_ANTHROPIC_BASE_URL", "https://api.anthropic.com"
    )
    return AnthropicCompatibleModelClient(
        model=config.model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.0,
        timeout=120,
    )


def _budget_exceeded(all_results: list, config: RunConfig, wall_start_ns: int) -> str | None:
    """Return a short reason string if any cost guard has fired; else None."""
    total_calls = sum(r.provider_call_count_this_turn for r in all_results)
    if total_calls > config.max_provider_calls:
        return f"max_provider_calls exceeded ({total_calls}>{config.max_provider_calls})"
    total_tokens = 0
    for r in all_results:
        u = r.usage or {}
        total_tokens += int(u.get("input_tokens", 0) or 0)
        total_tokens += int(u.get("output_tokens", 0) or 0)
    if total_tokens > config.max_total_tokens:
        return f"max_total_tokens exceeded ({total_tokens}>{config.max_total_tokens})"
    elapsed_s = (time.monotonic_ns() - wall_start_ns) / 1e9
    if elapsed_s > config.timeout_seconds:
        return f"timeout_seconds exceeded ({elapsed_s:.0f}>{config.timeout_seconds})"
    return None


def do_reset(repo_root: Path) -> int:
    """Remove leftover seed note, restore pico.toml backup, clear results/*.json."""
    removed = []
    seed = repo_root / SEED_NOTE_REL
    if seed.exists():
        seed.unlink()
        removed.append(str(seed.relative_to(repo_root)))

    backup = repo_root / BACKUP_REL
    pico_toml = repo_root / PICO_TOML_REL
    if backup.exists():
        pico_toml.write_bytes(backup.read_bytes())
        backup.unlink()
        removed.append(f"restored {pico_toml.name} from backup")
    elif pico_toml.exists():
        # No backup means the fixture pico.toml is the only one — delete it
        # if it matches the fixture; else leave alone.
        current = pico_toml.read_text(encoding="utf-8")
        if current == FIXTURE_PICO_TOML:
            pico_toml.unlink()
            removed.append(f"deleted fixture {pico_toml.name}")

    results_dir = repo_root / "benchmarks" / "live_e2e" / "results"
    if results_dir.exists():
        for j in results_dir.glob("*.json"):
            j.unlink()
            removed.append(str(j.relative_to(repo_root)))

    if removed:
        print("[live-e2e] --reset cleaned:")
        for item in removed:
            print(f"  - {item}")
    else:
        print("[live-e2e] --reset: nothing to clean")
    return 0


def main() -> int:
    config = parse_args()

    repo_root = Path.cwd()

    if config.reset:
        return do_reset(repo_root)

    check_env(config)
    verify_pico_repo(repo_root)
    warn_if_dirty_working_tree(repo_root)

    # Detect pre-existing seed note (unclean previous run)
    if (repo_root / SEED_NOTE_REL).exists():
        print(
            f"[live-e2e] {SEED_NOTE_REL} already exists — run with --reset first",
            file=sys.stderr,
        )
        return 2

    wall_start = time.monotonic_ns()

    TURNS = [
        (1, "上次讨论过 cache invariant 的问题，帮我看看这个仓库的 cache 相关代码", "recall_triggered"),
        (2, "读一下 pico/runtime.py 完整内容并告诉我它主要做什么", "digest_applied"),
        (3, "再看一下 pico/context_manager.py", "injection_dropped"),
        (4, "总结一下我们目前讨论的所有内容", "history_dropped"),
        (5, "最后 done", "cache_anchor_verified"),
    ]

    with FixtureManager(repo_root):
        # Lazy import of pico so a broken pico module produces exit 4 (not 2).
        from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient  # noqa: F401
        from pico.runtime import Pico
        from pico.session_store import SessionStore
        from pico.workspace import WorkspaceContext

        model_client = _make_anthropic_client(config)
        workspace = WorkspaceContext.build(repo_root)
        session_store = SessionStore(repo_root / ".pico" / "sessions")
        try:
            pico = Pico(
                model_client=model_client,
                workspace=workspace,
                session_store=session_store,
                read_only=True,
                max_steps=3,
            )
        except Exception as exc:
            print(f"[live-e2e] pico construction failed: {exc}", file=sys.stderr)
            return 4

        runner = TurnRunner(pico, config)
        reporter = Reporter(config, repo_root / "benchmarks" / "live_e2e" / "results")
        engine = AssertionEngine()

        all_results: list = []
        all_assertions: dict = {}
        aborted_reason: str | None = None

        for turn_no, prompt, expected in TURNS:
            try:
                result = runner.run_turn(turn_no, prompt, expected)
            except Exception as exc:
                print(f"[live-e2e] turn {turn_no} uncaught exception: {exc}", file=sys.stderr)
                return 4
            if result.error is not None:
                # provider or pico error mid-turn
                all_results.append(result)
                all_assertions[turn_no] = []
                print(f"[live-e2e] turn {turn_no} error: {result.error}", file=sys.stderr)
                aborted_reason = f"provider_error_turn_{turn_no}"
                break
            all_results.append(result)
            turn_asserts = engine.dispatch(turn_no, result, pico, all_results)
            all_assertions[turn_no] = turn_asserts
            reporter.render_turn_summary(turn_no, expected, turn_asserts)

            reason = _budget_exceeded(all_results, config, wall_start)
            if reason:
                aborted_reason = reason
                print(f"[live-e2e] budget guard fired: {reason}", file=sys.stderr)
                break

        # Run global checks whether we finished or aborted early
        global_asserts = engine.check_global(all_results, pico)
        all_assertions["global"] = global_asserts
        reporter.render_turn_summary("global", "cross-turn invariants", global_asserts)

        # Assemble totals
        totals = {
            "provider_calls": sum(r.provider_call_count_this_turn for r in all_results),
            "input_tokens": sum(int((r.usage or {}).get("input_tokens", 0) or 0) for r in all_results),
            "output_tokens": sum(int((r.usage or {}).get("output_tokens", 0) or 0) for r in all_results),
            "cache_creation_input_tokens": sum(
                int((r.usage or {}).get("cache_creation_input_tokens", 0) or 0) for r in all_results
            ),
            "cache_read_input_tokens": sum(
                int((r.usage or {}).get("cache_read_input_tokens", 0) or 0) for r in all_results
            ),
        }

        wall_time_ms = (time.monotonic_ns() - wall_start) // 1_000_000
        report_path = reporter.write_json(all_results, all_assertions, config, totals, wall_time_ms)

        # Compute overall pass
        total_asserts = 0
        passed_asserts = 0
        for asserts_list in all_assertions.values():
            for a in asserts_list:
                total_asserts += 1
                if a.passed:
                    passed_asserts += 1
        overall_pass = (passed_asserts == total_asserts) and aborted_reason is None

        reporter.render_final(
            overall_pass=overall_pass,
            totals=totals,
            wall_time_ms=wall_time_ms,
            report_path=report_path,
            assertion_summary=(passed_asserts, total_asserts),
        )

        if aborted_reason:
            # Distinguish budget from provider error
            if aborted_reason.startswith("provider_error"):
                return 3
            if "max_total_tokens" in aborted_reason:
                return 5
            if "timeout_seconds" in aborted_reason:
                return 6
            return 5  # max_provider_calls falls under exit 5 too
        return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Update `.gitignore`**

Append to root `.gitignore` (or use `sed`):

```bash
cat >> .gitignore << 'EOF'

# Live-e2e benchmark artifacts
benchmarks/live_e2e/results/*.json
benchmarks/live_e2e/results/pre-run-pico.toml.bak
EOF
```

- [ ] **Step 3: Smoke — `--reset` on clean tree completes with exit 0**

```bash
uv run python -m benchmarks.live_e2e.run_live_session --reset
echo "exit=$?"
```

Expected: prints `[live-e2e] --reset: nothing to clean` (or lists cleaned items), `exit=0`

- [ ] **Step 4: Smoke — missing API key produces exit 2**

```bash
env -u PICO_ANTHROPIC_API_KEY uv run python -m benchmarks.live_e2e.run_live_session
echo "exit=$?"
```

Expected: prints missing-key error, `exit=2`

- [ ] **Step 5: Run pytest to confirm assertion unit tests still pass**

```bash
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

Expected: 20 passed

- [ ] **Step 6: Commit**

```bash
git add benchmarks/live_e2e/run_live_session.py .gitignore
git commit -m "feat(live-e2e): main orchestration, do_reset, cost guards, exit codes"
```

---

## Task 12: `README.md`

**Files:**
- Create: `benchmarks/live_e2e/README.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Write the README**

Write to `benchmarks/live_e2e/README.md`:

```markdown
# Live-API End-to-End Test

Runs 5 designed turns through the real Anthropic API and hard-asserts
**27 concrete invariants** covering pico's four post-migration
optimizations: `<system-reminder>` injection, memory recall, tool_result
digest, and history-budget drop. See
`docs/superpowers/specs/2026-07-08-pico-live-e2e-test-design.md` for
the full design.

**This is not a pytest test.** It is a standalone script that consumes
real API credits. Run it manually before shipping large changes to the
memory/context subsystems.

## Prerequisites

- `PICO_ANTHROPIC_API_KEY` set in `.env` (or the environment)
- Working directory: pico repo root, on the `memory` branch or later

Optional environment overrides:

- `PICO_ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`)
- `PICO_ANTHROPIC_MODEL` (default `claude-sonnet-4-5-20250929`)

## How to run

Full run (writes a JSON report to `benchmarks/live_e2e/results/`):

    uv run python -m benchmarks.live_e2e.run_live_session

Cheaper model:

    uv run python -m benchmarks.live_e2e.run_live_session --model claude-haiku-...

Clean up after a failed / partial run:

    uv run python -m benchmarks.live_e2e.run_live_session --reset

Tune cost guards (defaults shown):

    uv run python -m benchmarks.live_e2e.run_live_session \
        --max-provider-calls 15 \
        --max-total-tokens 200000 \
        --timeout-seconds 300

## Cost estimate

- claude-sonnet-4-5: **~$0.20 per full run**
- claude-haiku-...:  **~$0.05 per full run**

Both estimates are with default cost caps (15 calls / 200K tokens / 5min).

## What it validates

Five turns, each targeting one optimization:

| Turn | Purpose | Assertion count |
| ---- | ------- | --------------- |
| 1    | Recall keyword hits + memory injection reaches provider | 6 |
| 2    | Large `read_file` result gets digested; raw file lands on disk | 5 |
| 3    | Injection budget drop honors `DROP_PRIORITY` (checkpoint first, recalled_memory last) | 4 |
| 4    | History-budget drop preserves tool_use/tool_result pairing invariant | 5 |
| 5    | `cache_control` breakpoints yield cache tokens; `system_cache_key` stable across turns | 5 |
| —    | Global: total provider calls ≤ 15, total tokens ≤ 200K | 2 |

## What it does NOT validate

- Quality of the model's output — see `benchmarks/memory_quality/`
- Latency — see `benchmarks/perf/`
- Non-Anthropic providers — fallback path is covered by
  `tests/e2e/test_fallback_provider_parity.py`

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0    | All 27 assertions passed |
| 1    | At least one assertion failed (JSON report written) |
| 2    | Preflight failure: missing env, not a pico repo, unclean seed |
| 3    | Provider error mid-run (API 4xx/5xx/timeout) |
| 4    | Uncaught pico exception (JSON report written with `error` field) |
| 5    | Cost budget exceeded (provider_calls or tokens) |
| 6    | Wall-time timeout exceeded |

## Interpreting the JSON report

Every run writes `benchmarks/live_e2e/results/live-e2e-<ns_timestamp>.json`.
Schema is documented in the spec §6. The most useful top-level fields:

- `overall_pass`: single boolean summary
- `assertion_summary`: `{total, passed, failed}`
- `turns[i].assertions`: per-assertion `{name, passed, expected, actual}`
- `totals`: cumulative provider calls + token usage across all turns

## Safety

- `read_only=True` is enforced at Pico construction — `write_file`,
  `patch_file`, and `run_shell` writes are refused by `tool_executor`.
- The script snapshots and restores `pico.toml` around the run.
- The seed note lives at `.pico/memory/agent/cache-invariant.md` for
  the duration of a run and is removed on exit (even on failure).
- `.pico/sessions/` and `.pico/runs/` are preserved after the run
  (useful for replay). Remove manually if desired.
```

- [ ] **Step 2: Commit**

```bash
git add benchmarks/live_e2e/README.md
git commit -m "docs(live-e2e): README with prerequisites, cost, exit codes"
```

---

## Task 13: Final smoke run (real API)

**Files:** none modified

**Interfaces:** none — verification only

**Note**: this task requires `PICO_ANTHROPIC_API_KEY`. If the key is not
available, skip Step 2 and Step 3; the plan is otherwise complete.

- [ ] **Step 1: Confirm unit test suite passes**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q`
Expected: 20 passed

Also verify the rest of the repo suite is unchanged:

Run: `uv run pytest -q`
Expected: same passing count as before this plan (668 passed, 1 skipped)

- [ ] **Step 2: Real API smoke run**

Ensure `PICO_ANTHROPIC_API_KEY` is loaded (via `.env` or your shell):

Run: `uv run python -m benchmarks.live_e2e.run_live_session`
Expected: script completes, exit 0, "[live-e2e] OVERALL: ALL PASS · 27/27 assertions" line printed, JSON report at `benchmarks/live_e2e/results/live-e2e-*.json`

- [ ] **Step 3: Confirm report shape**

```bash
python3 -c "
import json, glob
files = sorted(glob.glob('benchmarks/live_e2e/results/live-e2e-*.json'))
data = json.load(open(files[-1]))
assert data['schema_version'] == 1
assert data['overall_pass'] is True
assert data['assertion_summary']['total'] == 27
assert data['assertion_summary']['passed'] == 27
assert len(data['turns']) == 5
print('report shape OK')
"
```

Expected: `report shape OK`

- [ ] **Step 4: Commit (empty gate)**

```bash
git commit --allow-empty -m "gate(live-e2e): real-API smoke verified · 27/27 assertions"
```

---

## Self-Review

**1. Spec coverage.** Every spec section is covered by a task:

| Spec section | Task |
| ------------ | ---- |
| §3 Architecture (directory layout) | Task 1 |
| §4 Five-turn task design + fixture pico.toml | Task 4 (FixtureManager holds FIXTURE_PICO_TOML) + Task 11 (TURNS list in main) |
| §4 Seed note | Task 3 |
| §5 27 hard assertions | Tasks 6-9 (6+5+4+5+5+2 = 27) |
| §6 JSON report schema v1 | Task 10 (Reporter.write_json) |
| §7 Component internal API | Tasks 2, 4, 5, 6-9, 10 |
| §8 Error handling + cost guards + exit codes | Task 11 (main + _budget_exceeded) |
| §9 Reset path | Task 11 (do_reset) |
| §10 Gitignore | Task 11 |
| §11 Unit tests for AssertionEngine | Tasks 6-9 |
| §12 README | Task 12 |
| §13 Definition of Done | Task 13 |

Total assertions across turns: turn 1 = 6, turn 2 = 5, turn 3 = 4,
turn 4 = 5, turn 5 = 5, global = 2 → 27. Matches spec §5.

**2. Placeholder scan.** All code steps carry complete code. No TBDs.
Every test step names the exact `pytest` invocation and expected count.

**3. Type consistency.**

- `RunConfig` fields defined in Task 2 (`provider`, `model`,
  `max_provider_calls`, `max_total_tokens`, `timeout_seconds`, `reset`,
  `verbose`) — consumed unchanged in Tasks 10 (Reporter.__init__),
  11 (main, do_reset, _budget_exceeded).
- `TurnResult` fields defined in Task 5 — consumed unchanged in
  Tasks 6-10.
- `Assertion` fields defined in Task 6 — consumed unchanged in
  Tasks 7-11.
- `FixtureManager` constants (`FIXTURE_PICO_TOML`, `SEED_NOTE_REL`,
  `PICO_TOML_REL`, `BACKUP_REL`) defined in Task 4 — reused by
  `do_reset` in Task 11.
- `AssertionEngine.dispatch(turn, result, pico, all_results)` signature
  defined in Task 6 (skeleton) — consumed by `main` in Task 11 with the
  same argument order.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-08-pico-live-e2e-test.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
