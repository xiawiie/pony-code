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
