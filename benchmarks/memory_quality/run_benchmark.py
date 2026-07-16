"""Memory quality benchmark runner.

Reads `scenario_*.jsonl`, sets up isolated fixture repos, runs fake or live
Pico sessions, scores memory tool traces, and emits text or JSON summaries.

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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.evaluation.provider_benchmark import _make_provider_client  # noqa: E402
from benchmarks.evaluation.metrics_common import (  # noqa: E402
    _decode_json_object,
    _validate_record_header,
)
from pico.memory.block_store import BlockStore  # noqa: E402
from pico.memory.retrieval import Retrieval  # noqa: E402
from pico.providers.fake import FakeModelClient  # noqa: E402
from pico.runtime import Pico  # noqa: E402
from pico.state.session_store import SessionStore  # noqa: E402
from pico.workspace import WorkspaceContext  # noqa: E402


MEMORY_QUALITY_SCENARIO_FORMAT_VERSION = 1
MEMORY_QUALITY_RESULT_FORMAT_VERSION = 1
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
                data = _decode_json_object(line)
                _validate_record_header(
                    data,
                    "memory_quality_scenario",
                    MEMORY_QUALITY_SCENARIO_FORMAT_VERSION,
                )
            except ValueError as exc:
                raise ScenarioLoadError(f"{jsonl}:{line_number}: {exc}") from exc
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
    if len(parts) != 2 or parts[0] not in {"workspace", "user"}:
        raise ValueError(f"invalid setup note path: {rel_path}")
    scope = parts[0]
    sub_path = parts[1]
    if not sub_path or sub_path.startswith("/") or ".." in sub_path.split("/"):
        raise ValueError(f"invalid setup note path: {rel_path}")
    if sub_path == "agent_notes.md":
        root = (
            workspace / ".pico" / "memory"
            if scope == "workspace"
            else workspace / ".pico" / "benchmark-user-memory"
        )
        return root / "agent_notes.md"
    if sub_path.startswith("notes/") and sub_path.endswith(".md"):
        root = (
            workspace / ".pico" / "memory"
            if scope == "workspace"
            else workspace / ".pico" / "benchmark-user-memory"
        )
        return root / sub_path
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
    ws = Path(tempfile.mkdtemp(prefix="pico-memory-bench-", dir=str(parent_dir) if parent_dir else None)).resolve()
    (ws / "AGENTS.md").write_text("# Test project\n", encoding="utf-8")
    for rel, content in setup_notes:
        target = _setup_note_target(ws, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return ws


_MEMORY_HIT_RE = re.compile(
    r"^- (?P<path>[a-z]+/[A-Za-z0-9_./#-]+) \(score=(?P<score>[0-9.]+)\)"
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


def _score_absent_hits(row: dict, absent_paths: list[str], search_events: list[dict]) -> None:
    observed = [
        hit["path"]
        for event in search_events
        for hit in parse_memory_search_hits(event.get("result", ""))
    ]
    row["observed_hits"] = list(dict.fromkeys([*row["observed_hits"], *observed]))
    leaked = [path for path in absent_paths if path in observed]
    if leaked:
        _mark_fail(row, "stale or deleted memory was recalled: " + ", ".join(leaked))


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
        "category": str(scenario.get("category", "legacy") or "legacy"),
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
        absent = turn.get("expected_absent_hit")
        absent_paths = (
            [str(absent)]
            if absent
            else [str(path) for path in turn.get("expected_absent_hits", [])]
        )
        if absent_paths:
            _score_absent_hits(row, absent_paths, search_events)
        if turn.get("expected_tool") == "memory_save":
            _score_memory_save(row, scenario, turn, tool_events, Path(workspace))
    forbidden = {str(name) for name in scenario.get("forbidden_tools", [])}
    called_forbidden = sorted(forbidden & set(tool_calls))
    if called_forbidden:
        _mark_fail(row, "forbidden tool called: " + ", ".join(called_forbidden))
    return row


def _tool_call(name: str, args: dict) -> dict:
    return {"name": name, "arguments": dict(args)}


def _fake_search_query_for_turn(turn: dict) -> str:
    user = str(turn.get("user", "")).strip()
    expected_paths = []
    if turn.get("expected_search_hit"):
        expected_paths.append(str(turn["expected_search_hit"]))
    if turn.get("expected_search_hits_top"):
        expected_paths.extend(str(path) for path in turn["expected_search_hits_top"])
    stems = " ".join(Path(path).stem for path in expected_paths)
    return " ".join(part for part in (user, stems) if part)


def _fake_outputs_for_turn(turn: dict) -> list[object]:
    if turn.get("expected_no_search_hit"):
        return [
            _tool_call(
                "memory_search",
                {"query": str(turn.get("user", "")), "limit": 5},
            ),
            "No relevant memory found.",
        ]
    if turn.get("expected_tool") == "memory_save":
        return [
            _tool_call("memory_save", {"note": _expected_note_from_turn(turn)}),
            "Saved the memory note.",
        ]
    return [
        _tool_call("memory_search", {"query": _fake_search_query_for_turn(turn), "limit": 5}),
        "Checked memory.",
    ]


def _fake_outputs_for_scenario(scenario: dict) -> list[object]:
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
    agent = Pico(
        model_client=model_client,
        workspace=workspace_context,
        session_store=SessionStore(str(workspace / ".pico" / "sessions")),
        approval_policy="never",
        max_steps=8,
        max_output_tokens=512,
        depth=0,
        max_depth=0,
    )
    agent.memory_store = BlockStore(
        workspace / ".pico" / "memory",
        workspace / ".pico" / "benchmark-user-memory",
    )
    agent.memory_retrieval = Retrieval(agent.memory_store)
    if hasattr(agent.context_manager, "_refresher"):
        agent.context_manager._refresher = None
    agent.tools = agent._apply_tool_allowlist(agent.build_tools())
    return agent


def _read_latest_trace(agent: Pico) -> list[dict]:
    if agent.current_task_state is None:
        return []
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_calls = {}
    for message in agent.session.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_calls[block.get("id")] = {
                    "name": block.get("name"),
                    "args": block.get("input", {}),
                }
            elif block.get("type") == "tool_result":
                call = tool_calls.get(block.get("tool_use_id"))
                if call is not None:
                    call["result"] = block.get("content", "")
    completed = [call for call in tool_calls.values() if "result" in call]
    for event, call in zip(
        (item for item in events if item.get("event") == "tool_executed"),
        completed,
    ):
        event.update(call)
    return events


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
            "category": str(scenario.get("category", "legacy") or "legacy"),
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
    categories = {}
    for category in sorted({str(row.get("category", "legacy")) for row in rows}):
        category_rows = [row for row in rows if row.get("category", "legacy") == category]
        category_passed = sum(row.get("status") == "pass" for row in category_rows)
        categories[category] = {
            "total": len(category_rows),
            "passed": category_passed,
            "failed": len(category_rows) - category_passed,
            "pass_rate": category_passed / len(category_rows),
        }
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / total if total else 0.0,
        "by_category": categories,
    }


def build_payload(rows: list[dict], mode: str, provider: str) -> dict:
    payload = {
        "record_type": "memory_quality_result",
        "format_version": MEMORY_QUALITY_RESULT_FORMAT_VERSION,
        "mode": mode,
        "summary": summarize_rows(rows),
        "rows": rows,
    }
    if mode == "live":
        payload["provider"] = provider
    return payload


def validate_result(payload: dict) -> dict:
    _validate_record_header(
        payload,
        "memory_quality_result",
        MEMORY_QUALITY_RESULT_FORMAT_VERSION,
    )
    if not isinstance(payload.get("summary"), dict) or not isinstance(
        payload.get("rows"), list
    ):
        raise ValueError("invalid memory quality result")
    return payload


def load_result(path: Path) -> dict:
    return validate_result(
        _decode_json_object(Path(path).read_text(encoding="utf-8"))
    )


def render_text(payload: dict) -> str:
    validate_result(payload)
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


if __name__ == "__main__":
    sys.exit(main())
