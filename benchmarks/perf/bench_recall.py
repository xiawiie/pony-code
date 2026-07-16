"""Benchmark recall_for_turn latency."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pico.memory.block_store import BlockStore  # noqa: E402
from pico.memory.recall import recall_for_turn  # noqa: E402
from pico.memory.retrieval import Retrieval  # noqa: E402


def _populate(root, count):
    (root / "notes").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / "notes" / f"note-{i}.md").write_text(
            f"---\nname: note-{i}\ntype: feedback\ndescription: cache topic {i}\n---\n"
            f"Body {i} mentioning cache.\n",
            encoding="utf-8",
        )


def _make_agent(store, ret, recent_history):
    return SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": recent_history},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )


def main():
    scenarios = []
    for note_count in [10, 100]:
        for recent_name, recent_hist in [
            ("empty_recent", []),
            ("full_recent", [[f"workspace/notes/note-{i}.md" for i in range(5)]]),
        ]:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                _populate(root, note_count)
                store = BlockStore(workspace_root=root, user_root=root / "user")
                ret = Retrieval(store)
                agent = _make_agent(store, ret, list(recent_hist))
                result = bench(
                    f"recall/{note_count}notes/{recent_name}",
                    lambda: recall_for_turn(agent, "cache", budget_tokens=1000),
                    iterations=50,
                )
                scenarios.append(result)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        _populate(root, 512)
        store = BlockStore(workspace_root=root, user_root=root / "user")
        ret = Retrieval(store)
        agent = _make_agent(store, ret, [])

        def shared_snapshot_turn():
            agent.session["recently_recalled"] = []
            snapshot = ret.snapshot()
            # The same object supplies Memory index metadata and recall/link data.
            len(snapshot.raw_documents)
            return recall_for_turn(
                agent,
                "cache",
                budget_tokens=6_144,
                snapshot=snapshot,
            )

        def double_scan_reference():
            agent.session["recently_recalled"] = []
            len(ret.snapshot().raw_documents)
            return recall_for_turn(agent, "cache", budget_tokens=6_144)

        shared = bench(
            "memory/turn/512/shared_snapshot",
            shared_snapshot_turn,
            iterations=30,
        )
        reference = bench(
            "memory/turn/512/double_scan_reference",
            double_scan_reference,
            iterations=30,
        )
        scenarios.extend((shared, reference))
        improvement = 1.0 - shared["median_ns"] / max(1, reference["median_ns"])
    print(
        json.dumps(
            {
                "suite": "memory-recall-performance-report-only",
                "scenarios": scenarios,
                "snapshot_512": {
                    "scan_count_per_turn": 1,
                    "p95_target_ns": 103_000_000,
                    "p95_within_target": shared["p95_ns"] <= 103_000_000,
                    "median_improvement_ratio": improvement,
                    "median_improvement_target": 0.20,
                    "median_improvement_within_target": improvement >= 0.20,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
