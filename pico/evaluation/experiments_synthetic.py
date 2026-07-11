import tempfile
from pathlib import Path

from ..context.renderer import render_current_user_message
from ..features import memory as memorylib
from ..messages import make_tool_pair, message_content_text
from ..providers.fake import FakeModelClient
from ..runtime import Pico
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from .metrics_common import _safe_mean, _safe_ratio


def _seed_plain_messages(agent, count, prefix, payload_size):
    seeded = []
    for index in range(int(count)):
        seeded.append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"{prefix}-{index}-" + ("X" * int(payload_size)),
                "_pico_meta": {"created_at": f"2026-04-08T11:{index:02d}:00+00:00"},
            }
        )
    agent.session["messages"].extend(seeded)


def _sent_message_chars(request):
    return sum(len(message_content_text(message)) for message in request["messages"])


def _latest_plain_user(request):
    for message in reversed(request["messages"]):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _preview_request(agent, user_message):
    preview = {
        "role": "user",
        "content": user_message,
        "_pico_meta": {"created_at": "2026-07-10T00:00:00+00:00"},
    }
    original_session = agent.session
    preview_session = {
        **original_session,
        "messages": [*original_session.get("messages", []), preview],
        "recently_recalled": list(original_session.get("recently_recalled", [])),
    }
    if isinstance(original_session.get("_recall_errors"), dict):
        preview_session["_recall_errors"] = dict(original_session["_recall_errors"])
    agent.session = preview_session
    try:
        snapshot, telemetry = render_current_user_message(agent, user_message)
        return agent.context_manager.build_request(
            injection_snapshot=snapshot,
            injection_telemetry=telemetry,
            preflight_metadata={},
        )
    finally:
        agent.session = original_session


def measure_request_ablation_metrics(agent, user_message):
    original_cap = agent.context_config["history_soft_cap"]
    results = {}
    try:
        for name, cap in (("bounded", 4_000), ("unbounded", 1_000_000)):
            agent.context_config["history_soft_cap"] = cap
            request, metadata = _preview_request(agent, user_message)
            results[name] = {
                "request_chars": _sent_message_chars(request),
                "dropped_messages": int(metadata["dropped_messages"]),
                "current_request_preserved": user_message in _latest_plain_user(request),
            }
    finally:
        agent.context_config["history_soft_cap"] = original_cap
    return results


def _prompt_has_reusable_file_summary(prompt, expected_working_line):
    marker = "Recent working file summaries:"
    expected_working_line = str(expected_working_line).strip()
    if not expected_working_line:
        return False
    in_memory_index = False
    in_working_summaries = False
    for line in str(prompt).splitlines():
        line = line.strip()
        if line == "<pico:memory_index>":
            in_memory_index = True
            continue
        if in_memory_index and line == "</pico:memory_index>":
            return False
        if not in_memory_index:
            continue
        if line == marker:
            in_working_summaries = True
            continue
        if in_working_summaries and line == expected_working_line:
            return True
    return False


def build_stress_agent_metrics():
    with tempfile.TemporaryDirectory(prefix="pico-metrics-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(workspace_root)
        store = SessionStore(workspace_root / ".pico" / "sessions")
        agent = Pico(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
        )
        _seed_plain_messages(agent, 12, "stress-history", 1_400)
        metrics = measure_request_ablation_metrics(agent, "recall")
        bounded = metrics["bounded"]
        unbounded = metrics["unbounded"]
        return {
            "bounded_request_chars": bounded["request_chars"],
            "unbounded_request_chars": unbounded["request_chars"],
            "bounded_dropped_messages": bounded["dropped_messages"],
            "compression_ratio": _safe_ratio(
                unbounded["request_chars"] - bounded["request_chars"],
                unbounded["request_chars"],
            ),
            "current_request_preserved": bounded["current_request_preserved"],
        }


class _MemoryExperimentModelClient(FakeModelClient):
    def __init__(self, expected_fact, filename):
        super().__init__([])
        self.expected_fact = str(expected_fact).strip().lower()
        self.filename = str(filename).strip()
        self.phase = "bootstrap_tool"
        self.followup_reads = 0
        self.expected_working_line = ""
        self.followup_prompt = ""

    def complete(
        self, *, system, tools, messages, max_tokens, cache_breakpoints=None
    ):
        prompt = "\n".join(message_content_text(message) for message in messages)
        if self.phase == "bootstrap_tool":
            self.phase = "bootstrap_final"
            output = f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        elif self.phase == "bootstrap_final":
            self.phase = "question"
            output = "<final>Done.</final>"
        elif self.phase == "question":
            self.followup_prompt = str(prompt)
            if _prompt_has_reusable_file_summary(prompt, self.expected_working_line):
                output = f"<final>{self.expected_fact.capitalize()}.</final>"
            else:
                self.phase = "question_after_read"
                self.followup_reads += 1
                output = f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        elif self.phase == "question_after_read":
            self.phase = "done"
            output = f"<final>{self.expected_fact.capitalize()}.</final>"
        else:
            output = f"<final>{self.expected_fact.capitalize()}.</final>"
        self.outputs.append(output)
        return super().complete(
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            cache_breakpoints=cache_breakpoints,
        )


def _build_memory_experiment_agent(workspace_root, expected_fact, filename):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".pico" / "sessions")
    return Pico(
        model_client=_MemoryExperimentModelClient(expected_fact, filename),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def _set_irrelevant_memory(agent):
    summaries = agent.session.setdefault("memory", {}).setdefault("file_summaries", {})
    summaries.clear()
    memorylib.set_file_summary_dict(summaries, "other.txt", "team mascot is blue", workspace_root=agent.root)
    agent.memory.remember_file("other.txt")
    agent._sync_working_memory()


def _clear_file_summary_memory(agent):
    agent.session.setdefault("memory", {})["file_summaries"] = {}


def _age_bootstrap_messages(agent, filler_count=8):
    _seed_plain_messages(agent, filler_count, "memory-ablation-filler", 560)


def _bootstrap_tool_use_id(agent):
    for message in agent.session["messages"]:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_use" and block.get("id"):
                return block["id"]
        else:
            continue
    raise AssertionError("bootstrap read did not produce a tool use")


def _prepare_memory_followup(agent, filename):
    bootstrap_tool_use_id = _bootstrap_tool_use_id(agent)

    summary = agent.session["memory"]["file_summaries"][filename]["summary"]
    model_client = getattr(agent.model_client, "_inner", agent.model_client)
    model_client.expected_working_line = f"{filename} -> {summary}"
    _age_bootstrap_messages(agent)
    agent.context_config["history_soft_cap"] = 900
    agent.context_config["history_floor_messages"] = 2
    return bootstrap_tool_use_id, model_client


def _run_memory_variant(mode):
    with tempfile.TemporaryDirectory(prefix="pico-memory-experiment-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        (workspace_root / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
        agent = _build_memory_experiment_agent(workspace_root, "deploy key is red", "facts.txt")
        assert agent.ask("Read facts.txt and remember the key fact.") == "Done."
        bootstrap_tool_use_id, model_client = _prepare_memory_followup(agent, "facts.txt")

        if mode == "memory_off":
            agent.feature_flags["memory"] = False
            _clear_file_summary_memory(agent)
        elif mode == "memory_irrelevant":
            _set_irrelevant_memory(agent)

        result = agent.ask("What color is the deploy key?")
        task_state = agent.current_task_state
        return {
            "correct": result.strip().lower() == "deploy key is red.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(model_client.followup_reads),
            "bootstrap_tool_turn_dropped": bool(model_client.followup_prompt)
            and bootstrap_tool_use_id not in model_client.followup_prompt,
        }


def run_memory_dependency_experiment(repetitions=3):
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for _ in range(int(repetitions)):
        for variant in variants:
            variants[variant].append(_run_memory_variant(variant))

    results = {}
    for variant, rows in variants.items():
        results[variant] = {
            "repeated_reads": sum(row["repeated_reads"] for row in rows),
            "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
            "avg_attempts": _safe_mean(row["attempts"] for row in rows),
            "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
            "bootstrap_tool_turn_dropped": bool(rows) and all(
                row["bootstrap_tool_turn_dropped"] for row in rows
            ),
        }
    return results


MEMORY_EXPERIMENT_TASKS = [
    {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
    {"id": "fact_api", "category": "fact_lookup", "filename": "settings.txt", "fact": "api base path is /v1/internal"},
    {"id": "fact_budget", "category": "fact_lookup", "filename": "limits.txt", "fact": "default step budget is 6"},
    {"id": "fact_timeout", "category": "fact_lookup", "filename": "runtime.txt", "fact": "timeout ceiling is 120 seconds"},
    {"id": "edit_intro", "category": "edit_dependency", "filename": "README.md", "fact": "first bullet is the locked intro line"},
    {"id": "edit_token", "category": "edit_dependency", "filename": "sample.txt", "fact": "second token is placeholder"},
    {"id": "edit_field", "category": "edit_dependency", "filename": "config.txt", "fact": "fixed field name is benchmark_schema"},
    {"id": "edit_line", "category": "edit_dependency", "filename": "notes.txt", "fact": "locked marker is on line three"},
    {"id": "history_file", "category": "history_reference", "filename": "history.txt", "fact": "deploy fact came from facts.txt"},
    {"id": "history_line", "category": "history_reference", "filename": "history.txt", "fact": "benchmark note came from line two"},
    {"id": "history_token", "category": "history_reference", "filename": "history.txt", "fact": "placeholder token was beta"},
    {"id": "history_tool", "category": "history_reference", "filename": "history.txt", "fact": "inspection tool was read_file"},
]


def _write_memory_task_files(workspace_root, task):
    filename = task["filename"]
    payload = task["fact"]
    (workspace_root / filename).write_text(payload + "\n", encoding="utf-8")


def _bootstrap_prompt(task):
    return f"Read {task['filename']} and remember the key fact."


def _followup_prompt(task):
    if task["category"] == "fact_lookup":
        return f"What does {task['filename']} say?"
    if task["category"] == "edit_dependency":
        return f"Use the remembered constraint from {task['filename']} to continue without rereading."
    return f"What was the conclusion we already established from {task['filename']}?"


def _set_irrelevant_memory_for_task(agent):
    summaries = agent.session.setdefault("memory", {}).setdefault("file_summaries", {})
    summaries.clear()
    memorylib.set_file_summary_dict(summaries, "other.txt", "the team mascot is blue", workspace_root=agent.root)
    agent.memory.remember_file("other.txt")
    agent._sync_working_memory()


def _run_memory_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="pico-memory-large-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        _write_memory_task_files(workspace_root, task)
        agent = _build_memory_experiment_agent(workspace_root, task["fact"], task["filename"])
        assert agent.ask(_bootstrap_prompt(task)) == "Done."
        bootstrap_tool_use_id, model_client = _prepare_memory_followup(agent, task["filename"])
        if variant == "memory_off":
            agent.feature_flags["memory"] = False
            _clear_file_summary_memory(agent)
        elif variant == "memory_irrelevant":
            _set_irrelevant_memory_for_task(agent)
        result = agent.ask(_followup_prompt(task))
        task_state = agent.current_task_state
        return {
            "correct": result.strip().lower() == f"{task['fact']}.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(model_client.followup_reads),
            "bootstrap_tool_turn_dropped": bool(model_client.followup_prompt)
            and bootstrap_tool_use_id not in model_client.followup_prompt,
        }


def run_large_scale_memory_experiment(repetitions=5):
    repetitions = int(repetitions)
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for task in MEMORY_EXPERIMENT_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                row = _run_memory_task_variant(task, variant)
                row["task_id"] = task["id"]
                row["category"] = task["category"]
                variants[variant].append(row)
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
    return {
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
                "memory_hit_rate": _safe_ratio(sum(1 for row in rows if row["repeated_reads"] == 0), len(rows)),
                "bootstrap_tool_turn_dropped": bool(rows) and all(
                    row["bootstrap_tool_turn_dropped"] for row in rows
                ),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


def run_context_stress_matrix(repetitions=5):
    repetitions = int(repetitions)
    history_levels = [("short", 4), ("medium", 12), ("long", 24)]
    note_levels = [("low", 2), ("high", 10)]
    request_levels = [("short", "recall"), ("long", "recall the relevant benchmark fact without dropping the latest request details")]
    configs = []

    for history_label, history_count in history_levels:
        for note_label, note_count in note_levels:
            for request_label, request_text in request_levels:
                per_run = []
                for _ in range(repetitions):
                    with tempfile.TemporaryDirectory(prefix="pico-context-matrix-") as temp_dir:
                        workspace_root = Path(temp_dir).resolve()
                        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                        workspace = WorkspaceContext.build(workspace_root)
                        store = SessionStore(workspace_root / ".pico" / "sessions")
                        agent = Pico(
                            model_client=FakeModelClient([]),
                            workspace=workspace,
                            session_store=store,
                            approval_policy="auto",
                        )
                        _seed_plain_messages(agent, note_count, "matrix-note", 180)
                        _seed_plain_messages(agent, history_count, "matrix-history", 700)
                        metrics = measure_request_ablation_metrics(agent, request_text)
                        bounded = metrics["bounded"]
                        unbounded = metrics["unbounded"]
                        per_run.append(
                            {
                                "bounded_request_chars": bounded["request_chars"],
                                "unbounded_request_chars": unbounded["request_chars"],
                                "bounded_dropped_messages": bounded["dropped_messages"],
                                "compression_ratio": _safe_ratio(
                                    unbounded["request_chars"] - bounded["request_chars"],
                                    unbounded["request_chars"],
                                ),
                                "current_request_preserved": bounded["current_request_preserved"],
                            }
                        )
                configs.append(
                    {
                        "id": f"{history_label}-{note_label}-{request_label}",
                        "history_level": history_label,
                        "note_level": note_label,
                        "request_level": request_label,
                        "bounded_request_chars": _safe_mean(item["bounded_request_chars"] for item in per_run),
                        "unbounded_request_chars": _safe_mean(item["unbounded_request_chars"] for item in per_run),
                        "bounded_dropped_messages": _safe_mean(item["bounded_dropped_messages"] for item in per_run),
                        "compression_ratio": _safe_mean(item["compression_ratio"] for item in per_run),
                        "current_request_preserved_rate": _safe_ratio(
                            sum(1 for item in per_run if item["current_request_preserved"]),
                            len(per_run),
                        ),
                    }
                )
    ratios = [config["compression_ratio"] for config in configs]
    bounded_chars = [config["bounded_request_chars"] for config in configs]
    unbounded_chars = [config["unbounded_request_chars"] for config in configs]
    return {
        "config_count": len(configs),
        "configs": configs,
        "summary": {
            "avg_bounded_request_chars": _safe_mean(bounded_chars),
            "avg_unbounded_request_chars": _safe_mean(unbounded_chars),
            "avg_request_compression_ratio": _safe_mean(ratios),
            "max_request_compression_ratio": max(ratios) if ratios else 0.0,
            "min_request_compression_ratio": min(ratios) if ratios else 0.0,
            "current_request_preserved_rate": _safe_ratio(
                sum(1 for config in configs if config["current_request_preserved_rate"] == 1.0),
                len(configs),
            ),
        },
    }


def _security_agent(workspace_root, approval_policy="auto", read_only=False):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        read_only=read_only,
    )


def _scenario_invalid_patch_nonunique(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\nbeta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta", "new_text": "locked"})
    return dict(agent._last_tool_result_metadata)


def _scenario_invalid_patch_missing_field(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta"})
    return dict(agent._last_tool_result_metadata)


def _scenario_timeout_out_of_range(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": 121})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_command(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_delegate_task(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("delegate", {"task": "", "max_steps": 2})
    return dict(agent._last_tool_result_metadata)


def _scenario_path_escape_read(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "../outside.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_symlink_escape(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-symlink-target.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace_root / "linked.txt").symlink_to(outside)
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "linked.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_search_escape(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("search", {"pattern": "abc", "path": "../outside"})
    return dict(agent._last_tool_result_metadata)


def _scenario_approval_denied(workspace_root):
    agent = _security_agent(workspace_root, approval_policy="never")
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_read_only_block(workspace_root):
    agent = _security_agent(workspace_root, read_only=True)
    agent.run_tool("write_file", {"path": "x.txt", "content": "nope"})
    return dict(agent._last_tool_result_metadata)


def _scenario_repeated_call(workspace_root):
    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    args = {"path": "README.md", "start": 1, "end": 1}
    for index in range(2):
        result = agent.run_tool("read_file", args)
        agent.session["messages"].extend(make_tool_pair(
            name="read_file",
            arguments=args,
            tool_use_id=f"toolu_synthetic_{index}",
            result_content=result,
            created_at="2026-04-09T00:00:00+00:00",
            tool_status="ok",
            effect_class="read_only",
        ))
    agent.run_tool("read_file", args)
    return dict(agent._last_tool_result_metadata)


SECURITY_SCENARIOS = [
    ("path_escape_read", _scenario_path_escape_read),
    ("symlink_escape", _scenario_symlink_escape),
    ("search_escape", _scenario_search_escape),
    ("approval_denied_shell", _scenario_approval_denied),
    ("read_only_write", _scenario_read_only_block),
    ("repeated_identical_call", _scenario_repeated_call),
    ("patch_nonunique", _scenario_invalid_patch_nonunique),
    ("patch_missing_new_text", _scenario_invalid_patch_missing_field),
    ("timeout_out_of_range", _scenario_timeout_out_of_range),
    ("empty_delegate_task", _scenario_empty_delegate_task),
]


def run_security_experiment_suite(repetitions=3):
    repetitions = int(repetitions)
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}
    for scenario_id, runner in SECURITY_SCENARIOS:
        for _ in range(repetitions):
            with tempfile.TemporaryDirectory(prefix="pico-security-exp-") as temp_dir:
                workspace_root = Path(temp_dir).resolve()
                (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                metadata = runner(workspace_root)
                metadata["scenario_id"] = scenario_id
                rows.append(metadata)
                event = str(metadata.get("security_event_type", "")).strip()
                if event:
                    security_event_counts[event] = security_event_counts.get(event, 0) + 1
                error_code = str(metadata.get("tool_error_code", "")).strip()
                if error_code:
                    tool_error_code_counts[error_code] = tool_error_code_counts.get(error_code, 0) + 1
    return {
        "scenario_count": len(SECURITY_SCENARIOS),
        "runs": len(rows),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }
