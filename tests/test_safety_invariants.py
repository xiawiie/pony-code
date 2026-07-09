import os
import shlex
import sys
from unittest.mock import patch

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


def test_write_file_refuses_user_notes_path(tmp_path):
    # 路径级硬拦截：approval=auto 也不能通过；不是靠审批模式挡的。
    agent = build_agent(tmp_path, [], approval_policy="auto")
    target_rel = ".pico/memory/notes/malicious.md"
    target_abs = tmp_path / target_rel

    result = agent.run_tool(
        "write_file",
        {"path": target_rel, "content": "should not land"},
    )

    assert "refusing to write user note path" in result
    assert not target_abs.exists()


def test_patch_file_refuses_user_notes_path(tmp_path):
    # 预先手工放一份 user note；patch_file 也必须挡住。
    note_rel = ".pico/memory/notes/design.md"
    note_abs = tmp_path / note_rel
    note_abs.parent.mkdir(parents=True, exist_ok=True)
    original = "original body\n"
    note_abs.write_text(original, encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool(
        "patch_file",
        {"path": note_rel, "old_text": "original", "new_text": "tampered"},
    )

    assert "refusing to write user note path" in result
    assert note_abs.read_text(encoding="utf-8") == original


def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
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
        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
    ):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["GH_PAT"]


def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "pico.toml").write_text(
        "[model]\n"
        'name = "deepseek-chat"\n'
        'base_url = "https://api.deepseek.com/anthropic"\n'
        'api_key_env = "MODEL_API_KEY"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MODEL_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
    ):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["MODEL_API_KEY"]


def test_cli_build_agent_skips_malformed_project_env_lines_with_warning(tmp_path, capsys):
    class DummyModelClient:
        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "pico.toml").write_text(
        "[model]\n"
        'name = "deepseek-chat"\n'
        'base_url = "https://api.deepseek.com/anthropic"\n'
        'api_key_env = "MODEL_API_KEY"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "not a valid env line\nMODEL_API_KEY=sk-project-secret\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {}, clear=True), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
    ):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])
        agent = pico_cli.build_agent(args)
        secret_names = agent.secret_env_summary()["secret_env_names"]

    captured = capsys.readouterr()
    assert "warning: skipped invalid .env line 1" in captured.err
    assert secret_names == ["MODEL_API_KEY"]


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
    ), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
    ):
        args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = pico_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["PICO_CUSTOM_SECRET"]


def test_cli_no_input_makes_default_approval_non_interactive(tmp_path):
    class DummyModelClient:
        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch(
        "pico.cli.build_resolved_model_client",
        return_value=DummyModelClient(),
    ):
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
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


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
