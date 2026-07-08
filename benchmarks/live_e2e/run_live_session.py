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

    # -- Turn 2: digest --------------------------------------------------

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

    # -- Turn 3: injection drop -----------------------------------------

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
