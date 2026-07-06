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


def _validated_setup_notes(scenario: dict) -> list[tuple[str, str]]:
    setup_notes = scenario.get("setup_notes", {})
    if not isinstance(setup_notes, dict):
        raise ValueError(f"{scenario.get('id', '<unknown>')}: setup_notes must be an object")

    validated_notes = []
    for rel, content in setup_notes.items():
        rel_path = str(rel)
        _setup_note_target(Path("__pico_workspace__"), rel_path)
        validated_notes.append((rel_path, str(content)))
    return validated_notes


def setup_workspace(scenario: dict, parent_dir: Path | None = None) -> Path:
    parent_dir = Path(parent_dir) if parent_dir is not None else None
    setup_notes = _validated_setup_notes(scenario)
    ws = Path(tempfile.mkdtemp(prefix="pico-memory-bench-", dir=str(parent_dir) if parent_dir else None))
    (ws / "AGENTS.md").write_text("# Test project\n", encoding="utf-8")
    for rel, content in setup_notes:
        target = _setup_note_target(ws, rel)
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
