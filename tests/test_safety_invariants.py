import json
import os
import shlex
import subprocess
import sys
from types import MappingProxyType
from unittest.mock import Mock, patch

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico import cli as pico_cli
from pico.config import read_project_env
from pico.task_state import TaskState


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_workspace_bootstrap_ignores_workspace_git_from_path(tmp_path, monkeypatch):
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    runner = Mock(side_effect=AssertionError("workspace git executed"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("pico.safe_subprocess.subprocess.run", runner)

    workspace = WorkspaceContext.build(tmp_path)

    assert workspace.repo_root == str(tmp_path.resolve())
    assert workspace.trusted_executables == {}
    runner.assert_not_called()


def test_workspace_bootstrap_uses_hardened_git_and_drops_startup_log(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").mkdir()
    calls = []

    def fake_git(executable, args, **kwargs):
        calls.append((executable, list(args), kwargs))
        stdout = {
            ("rev-parse", "--show-toplevel"): str(repo),
            ("branch", "--show-current"): "topic\n",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main\n",
            ("status", "--short"): " M README.md\n",
        }[tuple(args)]
        return subprocess.CompletedProcess([executable, *args], 0, stdout=stdout, stderr="")

    monkeypatch.setattr("pico.workspace.run_hardened_git", fake_git)
    executables = {"git": "/trusted/git", "rg": "/trusted/rg"}

    workspace = WorkspaceContext.build(child, executables=executables)

    assert workspace.repo_root == str(repo.resolve())
    assert workspace.branch == "topic"
    assert workspace.status == "M README.md"
    assert workspace.recent_commits == []
    assert workspace.trusted_executables == executables
    assert calls[0][1] == ["rev-parse", "--show-toplevel"]
    assert calls[0][2]["cwd"] == repo.resolve()
    assert all(call[1][0] != "log" for call in calls)


def test_cli_freezes_parent_path_before_project_env_loading(tmp_path, monkeypatch):
    parent_path = os.environ.get("PATH", "")
    fake_path = str(tmp_path / "fake-bin")
    (tmp_path / ".env").write_text(
        f"PATH={fake_path}\nPICO_PROVIDER=ollama\n",
        encoding="utf-8",
    )
    observed = {}

    def capture_parent_path(workspace_root, *, env=None, names=()):
        observed["path"] = os.environ.get("PATH", "")
        return {}

    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr("pico.workspace.build_trusted_executables", capture_parent_path)
    monkeypatch.setattr("pico.cli.OllamaModelClient", DummyModelClient)
    args = pico_cli.build_arg_parser().parse_args([
        "--cwd",
        str(tmp_path),
        "--provider",
        "ollama",
    ])

    pico_cli.build_agent(args)

    assert observed["path"] == parent_path
    assert os.environ.get("PATH", "") == parent_path


def test_runtime_rejects_credential_bearing_base_url_before_client_construction(monkeypatch):
    def fail_client(*args, **kwargs):
        raise AssertionError("client constructed")

    monkeypatch.setattr(pico_cli, "AnthropicCompatibleModelClient", fail_client)
    args = pico_cli.build_arg_parser().parse_args([
        "--provider",
        "deepseek",
        "--base-url",
        "https://user:opaque-password@example.test/v1",
    ])

    with pytest.raises(ValueError, match="provider_base_url_credentials"):
        pico_cli._build_model_client(args)


def test_workspace_escape_is_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "../outside.txt"})

    assert "path escapes workspace" in result


def test_symlink_path_traversal_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "path escapes workspace" in result


def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: approval denied for run_shell"


def test_write_file_refuses_user_notes_path_before_runner(tmp_path):
    # 路径级硬拦截：approval=auto 也不能通过；不是靠审批模式挡的。
    agent = build_agent(tmp_path, [], approval_policy="auto")
    target_rel = ".pico/memory/notes/malicious.md"
    target_abs = tmp_path / target_rel

    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner
    result = agent.execute_tool(
        "write_file",
        {"path": target_rel, "content": "should not land"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert "refusing to write user note path" in result.content
    assert not target_abs.exists()
    runner.assert_not_called()


def test_patch_file_refuses_user_notes_path_before_runner(tmp_path):
    # 预先手工放一份 user note；patch_file 也必须挡住。
    note_rel = ".pico/memory/notes/design.md"
    note_abs = tmp_path / note_rel
    note_abs.parent.mkdir(parents=True, exist_ok=True)
    original = "original body\n"
    note_abs.write_text(original, encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    runner = Mock(return_value="must not run")
    agent.tools["patch_file"]["run"] = runner
    result = agent.execute_tool(
        "patch_file",
        {"path": note_rel, "old_text": "original", "new_text": "tampered"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert "refusing to write user note path" in result.content
    assert note_abs.read_text(encoding="utf-8") == original
    runner.assert_not_called()


def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
        "pico.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = pico_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--approval",
                "auto",
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = pico_cli.build_agent(args)
        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}


def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
        "pico.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["GH_PAT"]


def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PICO_DEEPSEEK_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch("pico.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["PICO_DEEPSEEK_API_KEY"]


def test_cli_resume_uses_immutable_collision_safe_snapshot_before_load(
    tmp_path,
    monkeypatch,
):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    old_secret = "opaque-process-old-value-123456789"
    preexisting_collision_secret = "opaque-preexisting-collision-123456789"
    project_secret = "opaque-project-only-value-123456789"
    session_id = "resume-safe"
    built_snapshot = {}
    original_build_snapshot = pico_cli._build_redaction_snapshot

    def capture_snapshot(*args, **kwargs):
        result = original_build_snapshot(*args, **kwargs)
        built_snapshot["value"] = result[0]
        return result

    monkeypatch.setattr(pico_cli, "_build_redaction_snapshot", capture_snapshot)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=ollama\n"
        "PICO_TEST_API_KEY=opaque-project-new-value-123456789\n"
        "PICO_SECRET_ENV_NAMES=PROJECT_ONLY_CREDENTIAL\n"
        f"PROJECT_ONLY_CREDENTIAL={project_secret}\n"
        "PICO_REDACTION_COLLISION_1_SECRET=synthetic-shadow-value-123456789\n",
        encoding="utf-8",
    )
    session_dir = tmp_path / ".pico" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.json").write_text(
        json.dumps({
            "id": session_id,
            "schema_version": 3,
            "messages": [{
                "role": "user",
                "content": (
                    old_secret
                    + " "
                    + project_secret
                    + " "
                    + preexisting_collision_secret
                ),
                "_pico_meta": {},
            }],
        }),
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "PICO_TEST_API_KEY": old_secret,
            "PICO_REDACTION_COLLISION_1_SECRET": preexisting_collision_secret,
        },
        clear=True,
    ), patch(
        "pico.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = pico_cli.build_arg_parser().parse_args([
            "--cwd",
            str(tmp_path),
            "--resume",
            session_id,
        ])
        agent = pico_cli.build_agent(args)

        assert "PROJECT_ONLY_CREDENTIAL" not in os.environ
        assert isinstance(agent.redaction_env, MappingProxyType)
        assert agent.redaction_env is built_snapshot["value"]
        assert project_secret in agent.redaction_env.values()
        assert old_secret in agent.redaction_env.values()
        assert preexisting_collision_secret in agent.redaction_env.values()
        with pytest.raises(TypeError):
            agent.redaction_env["MUTATE"] = "blocked"

    assert old_secret not in json.dumps(agent.session)
    assert project_secret not in json.dumps(agent.session)
    assert preexisting_collision_secret not in json.dumps(agent.session)


def test_cli_build_agent_skips_malformed_project_env_lines_with_warning(tmp_path, capsys):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "not a valid env line\nPICO_DEEPSEEK_API_KEY=sk-project-secret\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {}, clear=True), patch("pico.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = pico_cli.build_agent(args)
        secret_names = agent.secret_env_summary()["secret_env_names"]

    captured = capsys.readouterr()
    assert "warning: skipped invalid .env line 1" in captured.err
    assert secret_names == ["PICO_DEEPSEEK_API_KEY"]


def test_project_env_strips_unquoted_inline_comments(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_OPENAI_API_KEY=sk-project-secret # local key note\n"
        "PICO_OPENAI_MODEL=qwen3.7-max # default model\n"
        "PICO_OPENAI_API_BASE=\"https://example.test/v1 # literal\"\n"
        "PICO_LITERAL_HASH=abc#def\n",
        encoding="utf-8",
    )

    env = read_project_env(tmp_path, warn=False)

    assert env["PICO_OPENAI_API_KEY"] == "sk-project-secret"
    assert env["PICO_OPENAI_MODEL"] == "qwen3.7-max"
    assert env["PICO_OPENAI_API_BASE"] == "https://example.test/v1 # literal"
    assert env["PICO_LITERAL_HASH"] == "abc#def"


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        {
            "PICO_CUSTOM_SECRET": "custom-secret-value",
            "PICO_SECRET_ENV_NAMES": "PICO_CUSTOM_SECRET",
        },
        clear=True,
    ), patch("pico.cli.OllamaModelClient", DummyModelClient):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["PICO_CUSTOM_SECRET"]


def test_cli_no_input_makes_default_approval_non_interactive(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch("pico.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--no-input"])
        agent = pico_cli.build_agent(args)

    assert agent.approval_policy == "never"


def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="auto")
    script = 'import os; print(os.getenv("PICO_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"PICO_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_bound_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("pico.tools.subprocess.run") as fake_run:
        fake_run.return_value = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "toolkit-shell\n", "stderr": ""},
        )()
        shell_result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_run.assert_called_once()
    assert agent.tool_run_shell.__func__.__module__ == "pico.runtime"

    with patch("pico.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"task": "inspect README.md", "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()


def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"task": "inspect README.md", "max_steps": 2})
    except ValueError as exc:
        assert "delegate depth exceeded" in str(exc)
    else:
        raise AssertionError("delegate depth validation did not fail")


def test_delegate_child_is_read_only(tmp_path):
    target = tmp_path / "child-was-not-allowed.txt"
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"write a file","max_steps":2}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>child done</final>",
            "<final>parent done</final>",
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_uses = [
        message["content"][0]
        for message in agent.session["messages"]
        if message["role"] == "assistant"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_use"
    ]
    tool_results = [
        message["content"][0]
        for message in agent.session["messages"]
        if message["role"] == "user"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_result"
    ]
    assert tool_uses[0]["name"] == "delegate"
    assert "delegate_result" in tool_results[0]["content"]


def test_configured_secret_env_names_are_redacted_in_trace_and_report(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}, clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)
        agent.run_store.write_report(
            state,
            agent.redact_artifact({"task_state": state.to_dict(), "payload": payload}),
        )

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert github_pat not in report_text
    assert gh_pat not in report_text
    assert trace_text.count("<redacted>") >= 4
    assert report_text.count("<redacted>") >= 4
