"""Report Session Tree and compacted Context performance at 10k entries."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pony.agent.compaction import build_compaction_plan  # noqa: E402
from pony.context.renderer import InjectionSnapshot  # noqa: E402
from pony.agent.context_manager import ContextManager  # noqa: E402
from pony.agent.model_capabilities import (  # noqa: E402
    ModelCapabilities,
    TokenAccounting,
    build_model_budget,
)
from pony.state.session_store import SessionStore  # noqa: E402
from pony.workspace.context import now  # noqa: E402


SCENARIO_TARGETS = {
    "session/load/10000/cold": ("median_ns", 250_000_000),
    "session/append/10000/warm": ("p95_ns", 20_000_000),
    "compaction/plan/10000/warm": ("p95_ns", 100_000_000),
    "context/build/compacted-10000/warm": ("p95_ns", 50_000_000),
}
SCENARIO_NAMES = tuple(SCENARIO_TARGETS)


def _message(index):
    return {
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"message-{index} " + ("context " * 6),
        "_pony_meta": {"created_at": now()},
    }


def _session(root, count=10_000):
    return {
        "record_type": "session",
        "format_version": 2,
        "id": "session-perf",
        "created_at": now(),
        "workspace_root": str(root),
        "messages": [_message(index) for index in range(count)],
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }


def _with_target(result):
    metric, target = SCENARIO_TARGETS[result["name"]]
    return {
        **result,
        "target_metric": metric,
        "target_ns": target,
        "within_target": result[metric] <= target,
    }


def build_report():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        sessions_root = root / "sessions"
        store = SessionStore(sessions_root)
        store.save(_session(root))
        accounting = TokenAccounting()

        load_result = bench(
            "session/load/10000/cold",
            lambda: SessionStore(sessions_root).load_tree("session-perf"),
            iterations=12,
            warmup=2,
        )
        append_index = 10_000

        def append_one():
            nonlocal append_index
            store.append_messages("session-perf", (_message(append_index),))
            append_index += 1

        append_result = bench(
            "session/append/10000/warm",
            append_one,
            iterations=50,
        )
        tree = store.load_tree("session-perf")
        plan_result = bench(
            "compaction/plan/10000/warm",
            lambda: build_compaction_plan(
                tree,
                accounting,
                keep_recent_tokens=2_000,
            ),
            iterations=30,
        )

        plan = build_compaction_plan(
            tree,
            accounting,
            keep_recent_tokens=2_000,
        )
        store.append_control(
            "session-perf",
            "compaction",
            {
                "summary": "# Goal\nContinue the measured long session.",
                "first_kept_entry_id": plan.first_kept_entry_id,
                "tokens_before": plan.tokens_before,
                "summary_tokens": 12,
                "tail_tokens": plan.tail_tokens,
                "reason": "performance_benchmark",
                "compression_ratio": 0.05,
            },
        )
        capabilities = ModelCapabilities(
            context_window=128_000,
            max_output_tokens=16_384,
            token_counter_mode="provider_usage_or_estimate",
            source="config",
        )
        agent = SimpleNamespace(
            prefix="system",
            tools={},
            session=store.load("session-perf"),
            session_store=store,
            model_client=SimpleNamespace(supports_prompt_cache=False),
            token_accounting=accounting,
            model_budget=build_model_budget(capabilities),
            context_config={"compaction": {"enabled": True}},
            redaction_env={},
            secret_env_names=(),
            _pending_token_anchor=None,
        )
        manager = ContextManager(agent)
        snapshot = InjectionSnapshot(
            current_user="continue",
            runtime_feedback="",
            allocator_name="priority_allocator",
            sources=(),
        )
        build_result = bench(
            "context/build/compacted-10000/warm",
            lambda: manager.build_request(
                injection_snapshot=snapshot,
                injection_telemetry={},
                preflight_metadata={},
            ),
            iterations=50,
        )
        scenarios = [
            _with_target(load_result),
            _with_target(append_result),
            _with_target(plan_result),
            _with_target(build_result),
        ]
        return {
            "suite": "session-context-performance-report-only",
            "entry_count": len(tree.entries),
            "scenarios": scenarios,
            "all_within_target": all(row["within_target"] for row in scenarios),
        }


def main():
    print(json.dumps(build_report(), indent=2))


if __name__ == "__main__":
    main()
