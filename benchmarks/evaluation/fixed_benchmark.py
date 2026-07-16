import json
import locale as locale_module
import os
import shlex
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pico.memory.service as memorylib
from pico.agent.messages import validate_messages
from pico.agent.observability import load_run_artifacts
from pico.providers.fake import FakeModelClient
from pico.runtime import Pico
from pico.state.run_store import RunStore
from pico.state.session_store import SessionStore
from pico.tools.subprocess import (
    build_trusted_executables,
    run_hardened_command,
    run_hardened_git,
)
from pico.state.task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from pico.workspace import WorkspaceContext
from .benchmark_schema import (
    DEFAULT_BENCHMARK_PATH,
    _artifact_path_for_task,
    _digest_file,
    _fixture_snapshot_id,
    _scripted_outputs_for_task,
    _workspace_relative,
    load_benchmark,
    summarize_rows,
)

FIXED_BENCHMARK_RESULT_FORMAT_VERSION = 1
DEFAULT_ARTIFACT_PATH = Path("benchmarks/benchmark-v1.json")
DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_MODEL_NAME = "FakeModelClient"
DEFAULT_MODEL_VERSION = "scripted-deterministic"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_OUTPUT_TOKENS = 64
DEFAULT_TIMEZONE = "Asia/Shanghai"
REPRODUCIBILITY_LOCALE = "C.UTF-8"
_COMPACTION_SETUP = "compaction"


def _git_value(args, fallback="", cwd=None):
    try:
        result = run_hardened_git(
            "/usr/bin/git",
            args,
            cwd=cwd or Path.cwd(),
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _verifier_argv(command):
    argv = shlex.split(str(command), posix=True)
    if not argv:
        raise ValueError("empty verifier")
    shell_operators = {"&&", "||", ";", "|", "&", "<", ">", "<<", ">>"}
    if any(token in shell_operators for token in argv):
        raise ValueError("verifier shell operators are not allowed")
    return argv


def _run_verifier(command, *, cwd):
    argv = _verifier_argv(command)
    name = Path(argv[0]).name
    if name in {"python", "python3"} and Path("/usr/bin/python3").exists():
        executable = "/usr/bin/python3"
    else:
        trusted = build_trusted_executables(cwd, names=(name,))
        executable = trusted.get(name)
        if executable is None:
            raise ValueError("trusted verifier executable unavailable")
    return run_hardened_command(
        executable,
        args=argv[1:],
        cwd=cwd,
        timeout=30,
        env=_reproducibility_env(),
    )


def _current_locale():
    try:
        locale_module.setlocale(locale_module.LC_CTYPE)
    except Exception:
        pass
    return REPRODUCIBILITY_LOCALE


def _reproducibility_env():
    env = dict(os.environ)
    env["LC_ALL"] = REPRODUCIBILITY_LOCALE
    env["LANG"] = REPRODUCIBILITY_LOCALE
    return env


def _now_in_timezone(timezone_name):
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _checkpoint_payload(
    checkpoint_id,
    current_goal,
    next_step,
    runtime_identity,
    *,
    current_blocker="",
    key_files=None,
    freshness=None,
    summary="",
):
    return {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": "",
        "created_at": "2026-04-15T08:00:00+00:00",
        "goal": current_goal,
        "status": "in_progress",
        "completed": [],
        "in_progress": [current_goal],
        "blocker": current_blocker,
        "next_steps": [next_step] if next_step else [],
        "key_files": list(key_files or []),
        "read_files": [],
        "modified_files": [],
        "workspace_checkpoint_id": "",
        "worktree_identity_digest": "",
        "context_usage": {},
        "label": "benchmark-setup",
        "trigger": "benchmark_setup",
        "freshness": dict(freshness or {}),
        "summary": summary or current_goal,
        "runtime_identity": dict(runtime_identity),
    }


def _apply_task_setup(agent, task, fixture_copy_root):
    setup = dict(task.get("setup", {}) or {})
    if not setup:
        return

    kind = str(setup.get("kind", "")).strip()
    if kind == _COMPACTION_SETUP:
        history_count = int(setup.get("history_turns", setup.get("history_count", 12)))
        for index in range(history_count):
            agent.session["messages"].append(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"benchmark-history-{index}-" + ("A" * 220),
                    "_pico_meta": {
                        "created_at": f"2026-04-15T09:{index:02d}:00+00:00"
                    },
                }
            )
        agent.session_store.save(agent.session)
        agent.compact_session(
            focus="preserve the benchmark continuation state",
            reason="benchmark_setup",
            keep_recent_tokens=int(setup.get("keep_recent_tokens", 600)),
        )
        return

    if kind == "freshness_mismatch":
        path = str(setup.get("path", "sample.txt"))
        summary_text = str(setup.get("summary", f"{path}: stale benchmark summary"))
        summaries = agent.session.setdefault("memory", {}).setdefault("file_summaries", {})
        memorylib.set_file_summary_dict(summaries, path, summary_text, workspace_root=agent.root)
        agent.memory.remember_file(path)
        agent._sync_working_memory()
        freshness = summaries[agent.memory.canonical_path(path)]["freshness"]
        agent.session["checkpoints"] = {
            "current_id": "ckpt_freshness",
            "items": {
                "ckpt_freshness": _checkpoint_payload(
                    "ckpt_freshness",
                    current_goal="Re-anchor stale benchmark file state",
                    next_step=f"Re-read {path}",
                    runtime_identity={"workspace_fingerprint": agent.workspace.fingerprint()},
                    key_files=[{"path": path, "freshness": freshness}],
                    freshness={path: freshness},
                    summary="stale benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        (fixture_copy_root / path).write_text(str(setup.get("mutated_text", "alpha\nbeta\nstale-updated\nplaceholder\n")), encoding="utf-8")
        return

    if kind == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": _checkpoint_payload(
                    "ckpt_workspace",
                    current_goal="Recover after benchmark workspace drift",
                    next_step="Rebuild runtime state from a fresh checkpoint",
                    runtime_identity={"workspace_fingerprint": "outdated-benchmark-fingerprint"},
                    summary="workspace drift benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        return


def _agent_prompt_for_task(task):
    prompt = str(task["prompt"]).strip()
    expected = str(task.get("expected_artifact", "")).strip()
    verifier = str(task.get("verifier", "")).strip()
    if not expected and not verifier:
        return prompt
    lines = [prompt, "", "Success criteria:"]
    if expected:
        lines.append(f"- Expected artifact: {expected}")
    if verifier:
        lines.extend(
            [
                "- The run is successful only if this verification command passes and you return a final answer.",
                "- Do not run the verification command yourself unless run_shell is listed as an available tool.",
                "- If the verifier checks .pico runtime artifacts, return a final answer; those artifacts are produced after the run finishes.",
                "Verification command:",
                verifier,
            ]
        )
    return "\n".join(lines)


class BenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path=DEFAULT_BENCHMARK_PATH,
        artifact_path=DEFAULT_ARTIFACT_PATH,
        workspace_root=None,
        model_name=DEFAULT_MODEL_NAME,
        model_version=DEFAULT_MODEL_VERSION,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = (
            Path(workspace_root)
            if workspace_root is not None
            else Path(tempfile.mkdtemp(prefix="pico-benchmark-"))
        ).resolve()
        self.model_name = model_name
        self.model_version = model_version
        self.temperature = temperature
        self.top_p = top_p
        self.max_output_tokens = max_output_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory
        self.repo_root = self.benchmark_path.resolve().parent.parent

    def load(self):
        return load_benchmark(self.benchmark_path, repo_root=self.repo_root)

    def run(self):
        benchmark = self.load()
        rows = [self.run_task(task) for task in benchmark["tasks"]]
        summary = summarize_rows(rows)
        artifact = {
            "record_type": "fixed_benchmark_result",
            "format_version": FIXED_BENCHMARK_RESULT_FORMAT_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmark": {
                "source": str(self.benchmark_path.resolve().relative_to(self.repo_root)),
                "task_count": len(benchmark["tasks"]),
            },
            "reproducibility": {
                "fixture_snapshot_id": _fixture_snapshot_id(
                    self.repo_root / str(task["fixture_repo"]) for task in benchmark["tasks"]
                ),
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_output_tokens": self.max_output_tokens,
                },
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        fixture_source = self.repo_root / task["fixture_repo"]
        fixture_copy_root = self.workspace_root / task["id"] / fixture_source.name
        if fixture_copy_root.exists():
            shutil.rmtree(fixture_copy_root)
        fixture_copy_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, fixture_copy_root)

        workspace = WorkspaceContext.build(
            fixture_copy_root,
            repo_root_override=fixture_copy_root,
        )
        session_store = SessionStore(fixture_copy_root / ".pico" / "sessions")
        run_store = RunStore(fixture_copy_root / ".pico" / "runs")
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        else:
            model_client = FakeModelClient(_scripted_outputs_for_task(task))
        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_output_tokens=self.max_output_tokens,
            allowed_tools=task["allowed_tools"],
        )
        _apply_task_setup(agent, task, fixture_copy_root)

        initial_messages_empty = len(agent.session.get("messages", [])) == 0
        initial_task_summary_empty = not agent.memory.task_summary
        initial_episodic_notes_empty = True
        initial_memory_empty = initial_task_summary_empty and not agent.memory.recent_files

        final_answer = agent.ask(_agent_prompt_for_task(task))
        validate_messages(agent.session["messages"], require_meta=True)
        message_invariants_valid = True
        task_state = agent.current_task_state
        run_dir = Path(agent.current_run_dir)
        task_state_path = agent.run_store.task_state_path(task_state)
        report_path = agent.run_store.report_path(task_state)
        report, _trace = load_run_artifacts(agent.run_store.root, task_state.run_id)

        artifact_path = _artifact_path_for_task(task)
        artifact_file = fixture_copy_root / artifact_path
        expected_artifact_exists = artifact_file.exists()
        artifact_digest = _digest_file(artifact_file) if expected_artifact_exists else ""

        verifier = _run_verifier(task["verifier"], cwd=fixture_copy_root)

        within_budget = task_state.tool_steps <= int(task["step_budget"])
        verifier_passed = verifier.returncode == 0
        non_failure_stop_reason = task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
        passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
        failure_category = None if passed else self._failure_category(
            within_budget=within_budget,
            verifier_passed=verifier_passed,
            expected_artifact_exists=expected_artifact_exists,
            non_failure_stop_reason=non_failure_stop_reason,
        )

        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": _workspace_relative(fixture_copy_root, self.workspace_root),
            "run_id": task_state.run_id,
            "run_dir_relpath": _workspace_relative(run_dir, self.workspace_root),
            "task_state_relpath": _workspace_relative(task_state_path, self.workspace_root),
            "report_relpath": _workspace_relative(report_path, self.workspace_root),
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": artifact_path,
            "artifact_exists": expected_artifact_exists,
            "artifact_digest": artifact_digest,
            "verifier": task["verifier"],
            "verifier_exit_code": verifier.returncode,
            "verifier_stdout": verifier.stdout,
            "verifier_stderr": verifier.stderr,
            "category": task["category"],
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": failure_category,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "expected_artifact_exists": expected_artifact_exists,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "final_answer": final_answer,
            "stop_reason": task_state.stop_reason,
            "initial_messages_empty": initial_messages_empty,
            "message_invariants_valid": message_invariants_valid,
            "initial_memory_empty": initial_memory_empty,
            "initial_task_summary_empty": initial_task_summary_empty,
            "initial_episodic_notes_empty": initial_episodic_notes_empty,
            "task_state": task_state.to_dict(),
            "report": report,
        }

    def _failure_category(
        self,
        within_budget,
        verifier_passed,
        expected_artifact_exists,
        non_failure_stop_reason,
    ):
        if not expected_artifact_exists:
            return "missing_artifact"
        if not within_budget:
            return "budget_exceeded"
        if not verifier_passed:
            return "verifier_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        return "unknown"

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_fixed_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
    return evaluator.run()


def run_harness_regression_v2(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    return run_fixed_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
