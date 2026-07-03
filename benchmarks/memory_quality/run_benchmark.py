"""Memory quality benchmark runner (release gate, not CI).

Reads `scenario_*.jsonl`, sets up fixture repos, and reports the expected
success metric so a human reviewer can score the LLM's response. The
actual model invocation + tool-trace capture is wired in during a
release; this scaffold materializes workspaces and defines the summary
shape.

Usage:
    python benchmarks/memory_quality/run_benchmark.py [--scenario <substring>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


SCENARIO_DIR = Path(__file__).parent


def load_scenarios(filter_id: str | None = None):
    for jsonl in sorted(SCENARIO_DIR.glob("scenario_*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if filter_id and filter_id not in data.get("id", ""):
                continue
            yield jsonl.stem, data


def setup_workspace(scenario: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="pico-memory-bench-"))
    (ws / "AGENTS.md").write_text("# Test project\n", encoding="utf-8")
    for rel, content in scenario.get("setup_notes", {}).items():
        parts = rel.split("/", 1)
        if len(parts) != 2 or parts[0] != "workspace":
            continue
        target = ws / ".pico" / "memory" / parts[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return ws


def run_scenario(scenario_name: str, scenario: dict, keep: bool) -> dict:
    print(f"[{scenario_name}] {scenario['id']} ...", flush=True)
    ws = setup_workspace(scenario)
    try:
        print(f"  workspace: {ws}")
        print(f"  expected metric: {scenario['success_metric']}")
        # Real LLM invocation lives in the release harness; the scaffold
        # covers scenario loading + workspace materialization so future
        # wiring can focus on the tool-trace assertion path.
        return {"id": scenario["id"], "status": "scaffold_only", "workspace": str(ws)}
    finally:
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None, help="Filter by id substring.")
    ap.add_argument("--keep", action="store_true", help="Keep temp workspaces.")
    args = ap.parse_args()

    results = []
    for stem, scenario in load_scenarios(filter_id=args.scenario):
        results.append(run_scenario(stem, scenario, keep=args.keep))

    print("\n=== summary ===")
    for record in results:
        print(f"  {record['id']}: {record['status']}")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
