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
import re
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


_MEMORY_HIT_RE = re.compile(
    r"^- (?P<path>[a-z]+/[A-Za-z0-9_./-]+) \(score=(?P<score>[0-9.]+)\)"
)


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


def _mark_fail(row: dict, reason: str) -> None:
    if row["status"] != "fail":
        row["status"] = "fail"
        row["failure_reason"] = reason


def _append_expected_hits(row: dict, expected_hits: list[str]) -> None:
    for path in expected_hits:
        if path not in row["expected_hits"]:
            row["expected_hits"].append(path)


def _memory_save_note_arg(event: dict) -> str:
    args = event.get("args")
    if not isinstance(args, dict):
        return ""
    return str(args.get("note", ""))


def _score_expected_hits(row: dict, expected_hits: list[str], search_events: list[dict]) -> None:
    observed = []
    top_paths_by_event = []
    for event in search_events:
        hits = parse_memory_search_hits(event.get("result", ""))
        observed.extend(hit["path"] for hit in hits)
        top_paths_by_event.append([hit["path"] for hit in hits[:3]])
    _append_expected_hits(row, expected_hits)
    row["observed_hits"] = observed
    if not any(all(path in top_paths for path in expected_hits) for top_paths in top_paths_by_event):
        _mark_fail(row, "missing expected memory hit: " + ", ".join(expected_hits))


def _score_no_noise(row: dict, search_events: list[dict]) -> None:
    hits = []
    for event in search_events:
        hits.extend(parse_memory_search_hits(event.get("result", "")))
    row["observed_hits"] = [hit["path"] for hit in hits]
    if any(hit["score"] >= 1.0 for hit in hits):
        _mark_fail(row, "unexpected high-scoring memory hit")


def _score_memory_save(
    row: dict,
    scenario: dict,
    turn: dict,
    tool_events: list[dict],
    workspace: Path,
) -> None:
    save_events = [
        event
        for event in tool_events
        if event.get("name") == "memory_save" and event.get("tool_status") == "ok"
    ]
    notes_text = _agent_notes_text(workspace)
    expected_note = _expected_note_from_turn(turn)
    seeded_text = _seeded_agent_notes_text(scenario)
    row["agent_notes_changed"] = bool(notes_text and notes_text != seeded_text)
    if not save_events:
        _mark_fail(row, "successful memory_save was not called")
        return
    if expected_note and not any(expected_note in _memory_save_note_arg(event) for event in save_events):
        _mark_fail(row, "memory_save note args did not include expected note")
        return
    if expected_note and expected_note not in notes_text:
        _mark_fail(row, "expected note was not saved")
        return
    if seeded_text and seeded_text.strip() and seeded_text.strip() not in notes_text:
        _mark_fail(row, "existing agent note was not preserved")


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
