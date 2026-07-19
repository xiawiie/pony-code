import json
import os
import shlex
import subprocess
from types import MappingProxyType
from unittest.mock import Mock, patch

import pytest

from pony import Pony
from pony.cli import assembly as cli_assembly
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.cli import app as pony_cli
from pony.cli.errors import CliError
from benchmarks.support.fake_provider import FakeModelClient
from pony.config.environment import read_project_env
from pony.state.session_store import LEGACY_SESSION_FORMAT_VERSION
from pony.state.task_state import TaskState
from pony.runtime.options import RuntimeOptions
from pony.security.trust import ProjectTrustStore


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace_executables = kwargs.pop("workspace_executables", None)
    if workspace_executables is None:
        workspace = build_workspace(tmp_path)
    else:
        (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(
            tmp_path,
            executables=workspace_executables,
        )
    store = SessionStore(tmp_path / ".pony" / "sessions")
    permission_mode = kwargs.pop("permission_mode", "auto")
    agent = Pony(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True, **kwargs),
    )
    if permission_mode != "auto":
        agent.set_permission_mode(permission_mode)
    return agent


def build_cli_agent(args, tmp_path):
    return pony_cli.build_agent(
        args,
        trust_store=ProjectTrustStore(tmp_path / ".pony-home"),
        confirm=lambda _root: True,
    )


def test_workspace_bootstrap_ignores_workspace_git_from_path(tmp_path, monkeypatch):
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    runner = Mock(side_effect=AssertionError("workspace git executed"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("pony.tools.subprocess.subprocess.run", runner)

    workspace = WorkspaceContext.build(tmp_path)

    assert workspace.repo_root == str(tmp_path.resolve())
    assert workspace.trusted_executables == {}
    runner.assert_not_called()


def test_workspace_bootstrap_uses_hardened_git_and_drops_startup_log(
    tmp_path, monkeypatch
):
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
        return subprocess.CompletedProcess(
            [executable, *args], 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr("pony.workspace.context.run_hardened_git", fake_git)
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


def test_workspace_bootstrap_never_accepts_reported_root_outside_lexical_repo(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / ".git").mkdir()

    def fake_git(executable, args, **kwargs):
        stdout = {
            ("rev-parse", "--show-toplevel"): f"{outside}\n",
            ("branch", "--show-current"): "topic\n",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main\n",
            ("status", "--short"): "",
        }[tuple(args)]
        return subprocess.CompletedProcess(
            [executable, *args], 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr("pony.workspace.context.run_hardened_git", fake_git)

    workspace = WorkspaceContext.build(repo, executables={"git": "/trusted/git"})

    assert workspace.repo_root == str(repo.resolve())


def test_cli_freezes_parent_path_before_project_env_loading(tmp_path, monkeypatch):
    parent_path = os.environ.get("PATH", "")
    fake_path = str(tmp_path / "fake-bin")
    (tmp_path / ".env").write_text(
        f"PATH={fake_path}\n"
        "PONY_PROVIDER=openai-chat\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=test-key\n",
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

    monkeypatch.setattr(
        "pony.workspace.context.build_trusted_executables", capture_parent_path
    )
    monkeypatch.setattr(
        "pony.cli.assembly.build_transport_client",
        DummyModelClient,
    )
    args = pony_cli.build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
        ]
    )

    build_cli_agent(args, tmp_path)

    assert observed["path"] == parent_path
    assert os.environ.get("PATH", "") == parent_path


def test_runtime_preserves_frozen_executables_across_refresh(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    frozen = {"git": "/frozen/git", "rg": "/frozen/rg"}
    workspace = WorkspaceContext.build(tmp_path, executables=frozen)
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        "pony.workspace.context.build_trusted_executables",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime PATH rescan")
        ),
    )

    agent.refresh_prefix(force=True)

    assert dict(agent.trusted_executables) == frozen
    assert dict(agent.workspace.trusted_executables) == frozen
    assert dict(agent.workspace_observer.trusted_executables) == frozen
    assert dict(agent.tool_context().trusted_executables) == frozen
    with pytest.raises(TypeError):
        agent.trusted_executables["git"] = "/changed/git"


def test_delegate_inherits_parent_frozen_executables(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    frozen = {"git": "/frozen/git", "rg": "/frozen/rg"}
    workspace = WorkspaceContext.build(tmp_path, executables=frozen)
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            delegate_model_client_factory=lambda: FakeModelClient([]),
        ),
    )
    children = []

    def fake_ask(child, task):
        children.append(child)
        return "safe"

    monkeypatch.setattr(Pony, "ask", fake_ask)
    workspace.trusted_executables.clear()

    assert agent.spawn_delegate({"task": "inspect", "max_steps": 1}) == (
        "delegate_result:\nsafe"
    )
    child = children[0]
    assert child.model_client is not agent.model_client
    assert child.session_store is not agent.session_store
    assert child.run_store is not agent.run_store
    assert child.workspace is agent.workspace
    assert dict(child.trusted_executables) == frozen
    assert dict(child.workspace_observer.trusted_executables) == frozen
    assert dict(child.tool_context().trusted_executables) == frozen


def test_delegate_rejects_a_factory_that_returns_the_parent_client(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.delegate_model_client_factory = lambda: agent.model_client

    with pytest.raises(ValueError, match="reused the parent client"):
        agent.spawn_delegate({"task": "inspect", "name": "reviewer", "max_steps": 1})


def test_named_delegate_keeps_its_artifacts_outside_the_parent_stores(
    tmp_path, monkeypatch
):
    children = []
    agent = build_agent(
        tmp_path,
        [],
        delegate_model_client_factory=lambda: FakeModelClient([]),
    )

    def fake_ask(child, task):
        children.append((child, task))
        return "review complete"

    monkeypatch.setattr(Pony, "ask", fake_ask)
    result = agent.spawn_delegate(
        {"task": "inspect", "name": "reviewer", "max_steps": 1}
    )

    child, task = children[0]
    assert task == "inspect"
    assert result == "delegate_result[reviewer]:\nreview complete"
    assert child.current_permission_mode() == "dontAsk"
    assert child.session["id"].startswith("delegate-reviewer-")
    assert child.session_store.root.parent != agent.session_store.root
    assert child.run_store.root.parent != agent.run_store.root
    assert "delegate" not in child.visible_tools()
    assert not {"run_shell", "write_file", "patch_file", "memory_save", "write_plan"} & set(
        child.visible_tools()
    )
    for tool_name, tool_args in (
        ("memory_save", {"note": "do not persist"}),
        ("write_plan", {"plan": "# Plan"}),
        ("write_file", {"path": "blocked.txt", "content": "blocked"}),
    ):
        result = child.execute_tool(tool_name, tool_args)
        assert result.metadata["tool_error_code"] == "read_only_block"


def test_runtime_rejects_credential_bearing_base_url_before_client_construction(
    monkeypatch,
):
    def fail_client(*args, **kwargs):
        raise AssertionError("client constructed")

    monkeypatch.setattr(cli_assembly, "build_transport_client", fail_client)
    args = pony_cli.build_arg_parser().parse_args([])

    with pytest.raises(ValueError, match="api_base_credentials"):
        cli_assembly._build_transport_client(
            args,
            project_env={
                "PONY_PROVIDER": "anthropic",
                "PONY_API_BASE": "https://user:opaque-password@example.test/v1",
                "PONY_MODEL": "claude-test",
                "PONY_API_KEY": "test-key",
            },
            process_env={},
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

    assert result == "error: workspace_entry_unsafe"


def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], permission_mode="dontAsk")

    result = agent.run_tool("run_shell", {"command": "pwd", "timeout": 20})

    assert result == "error: permission mode 'dontAsk' blocks run_shell"


def test_write_file_refuses_user_notes_path_before_runner(tmp_path):
    # 路径级硬拦截：approval=auto 也不能通过；不是靠审批模式挡的。
    agent = build_agent(tmp_path, [], permission_mode="auto")
    target_rel = ".pony/memory/notes/malicious.md"
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
    note_rel = ".pony/memory/notes/design.md"
    note_abs = tmp_path / note_rel
    note_abs.parent.mkdir(parents=True, exist_ok=True)
    original = "original body\n"
    note_abs.write_text(original, encoding="utf-8")
    agent = build_agent(tmp_path, [], permission_mode="auto")

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
    with (
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "GITHUB_PAT": "ghp-1",
                "GH_PAT": "ghp-2",
                "PONY_PROVIDER": "openai-chat",
                "PONY_API_BASE": "https://gateway.example/v1",
                "PONY_MODEL": "claude-test",
                "PONY_API_KEY": "test-runtime-key",
            },
            clear=True,
        ),
        patch(
            "pony.cli.assembly.build_transport_client",
            DummyModelClient,
        ),
    ):
        args = pony_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = build_cli_agent(args, tmp_path)
        assert set(agent.secret_env_summary()["secret_env_names"]) == {
            "GITHUB_PAT",
            "GH_PAT",
            "PONY_API_KEY",
        }


def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with (
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "GH_PAT": "ghp-default-1",
                "PONY_PROVIDER": "openai-chat",
                "PONY_API_BASE": "https://gateway.example/v1",
                "PONY_MODEL": "claude-test",
                "PONY_API_KEY": "test-runtime-key",
            },
            clear=True,
        ),
        patch(
            "pony.cli.assembly.build_transport_client",
            DummyModelClient,
        ),
    ):
        args = pony_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
            ]
        )
        agent = build_cli_agent(args, tmp_path)
        assert set(agent.secret_env_summary()["secret_env_names"]) == {
            "GH_PAT",
            "PONY_API_KEY",
        }


def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=openai-chat\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=sk-project-secret\n",
        encoding="utf-8",
    )
    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch(
            "pony.cli.assembly.build_transport_client",
            DummyModelClient,
        ),
    ):
        args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])
        agent = build_cli_agent(args, tmp_path)
        assert agent.secret_env_summary()["secret_env_names"] == ["PONY_API_KEY"]


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
    original_build_snapshot = cli_assembly._build_redaction_snapshot

    def capture_snapshot(*args, **kwargs):
        result = original_build_snapshot(*args, **kwargs)
        built_snapshot["value"] = result[0]
        return result

    monkeypatch.setattr(cli_assembly, "_build_redaction_snapshot", capture_snapshot)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=openai-chat\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=test-runtime-key\n"
        "PONY_TEST_API_KEY=opaque-project-new-value-123456789\n"
        "PONY_SECRET_ENV_NAMES=PROJECT_ONLY_CREDENTIAL\n"
        f"PROJECT_ONLY_CREDENTIAL={project_secret}\n"
        "PONY_REDACTION_COLLISION_1_SECRET=synthetic-shadow-value-123456789\n",
        encoding="utf-8",
    )
    session_dir = tmp_path / ".pony" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / ".session_store.lock").touch(mode=0o600)
    (session_dir / f"{session_id}.json").write_text(
        json.dumps(
            {
                "record_type": "session",
                "format_version": LEGACY_SESSION_FORMAT_VERSION,
                "id": session_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "workspace_root": str(tmp_path),
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            old_secret
                            + " "
                            + project_secret
                            + " "
                            + preexisting_collision_secret
                        ),
                        "_pony_meta": {},
                    }
                ],
                "working_memory": {},
                "memory": {},
                "recently_recalled": [],
                "checkpoints": {},
                "resume_state": {},
                "runtime_identity": {},
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "PONY_TEST_API_KEY": old_secret,
                "PONY_REDACTION_COLLISION_1_SECRET": preexisting_collision_secret,
            },
            clear=True,
        ),
        patch(
            "pony.cli.assembly.build_transport_client",
            DummyModelClient,
        ),
    ):
        args = pony_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--resume",
                session_id,
            ]
        )
        agent = build_cli_agent(args, tmp_path)

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


def test_cli_build_agent_skips_malformed_project_env_lines_with_warning(
    tmp_path, capsys
):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "not a valid env line\n"
        "PONY_PROVIDER=openai-chat\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=sk-project-secret\n",
        encoding="utf-8",
    )
    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch(
            "pony.cli.assembly.build_transport_client",
            DummyModelClient,
        ),
    ):
        args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])
        agent = build_cli_agent(args, tmp_path)
        secret_names = agent.secret_env_summary()["secret_env_names"]

    captured = capsys.readouterr()
    assert "warning: skipped invalid .env line 1" in captured.err
    assert secret_names == ["PONY_API_KEY"]


def test_project_env_strips_unquoted_inline_comments(tmp_path):
    (tmp_path / ".env").write_text(
        "PONY_OPENAI_API_KEY=sk-project-secret # local key note\n"
        "PONY_OPENAI_MODEL=qwen3.7-max # default model\n"
        'PONY_OPENAI_API_BASE="https://example.test/v1 # literal"\n'
        "PONY_LITERAL_HASH=abc#def\n",
        encoding="utf-8",
    )

    env = read_project_env(tmp_path, warn=False)

    assert env["PONY_OPENAI_API_KEY"] == "sk-project-secret"
    assert env["PONY_OPENAI_MODEL"] == "qwen3.7-max"
    assert env["PONY_OPENAI_API_BASE"] == "https://example.test/v1 # literal"
    assert env["PONY_LITERAL_HASH"] == "abc#def"


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with (
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "PONY_CUSTOM_SECRET": "custom-secret-value",
                "PONY_SECRET_ENV_NAMES": "PONY_CUSTOM_SECRET",
                "PONY_PROVIDER": "openai-chat",
                "PONY_API_BASE": "https://gateway.example/v1",
                "PONY_MODEL": "claude-test",
                "PONY_API_KEY": "test-runtime-key",
            },
            clear=True,
        ),
        patch("pony.cli.assembly.build_transport_client", DummyModelClient),
    ):
        args = pony_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
            ]
        )
        agent = build_cli_agent(args, tmp_path)
        assert set(agent.secret_env_summary()["secret_env_names"]) == {
            "PONY_CUSTOM_SECRET",
            "PONY_API_KEY",
        }


def test_cli_no_input_fails_closed_for_untrusted_project(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with (
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "PONY_PROVIDER": "openai-chat",
                "PONY_API_BASE": "https://gateway.example/v1",
                "PONY_MODEL": "claude-test",
                "PONY_API_KEY": "test-runtime-key",
            },
            clear=True,
        ),
        patch("pony.cli.assembly.build_transport_client", DummyModelClient),
    ):
        args = pony_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--no-input",
            ]
        )
        with pytest.raises(CliError) as caught:
            pony_cli.build_agent(args)

    assert caught.value.code == "project_untrusted"
    assert not (tmp_path / ".pony" / "trusted-projects.json").exists()


def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(
        tmp_path,
        [],
        permission_mode="default",
        workspace_executables={"python": "/usr/bin/python3"},
    )
    agent.approve = lambda name, args: True
    script = 'import os; print(os.getenv("PONY_ALLOWLIST_SECRET", "missing"))'
    command = f"python -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"PONY_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_pony_exposes_no_raw_tool_runner_proxies(tmp_path):
    agent = build_agent(tmp_path, [], permission_mode="auto")

    for name in (
        "tool_list_files",
        "tool_read_file",
        "tool_search",
        "tool_run_shell",
        "tool_write_file",
        "tool_patch_file",
        "tool_delegate",
    ):
        assert not callable(getattr(agent, name, None)), name
    assert agent.tool_executor.agent is agent
    assert (
        "# README.md"
        in agent.execute_tool(
            "read_file",
            {"path": "README.md", "start": 1, "end": 1},
        ).content
    )


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
            {"name": "delegate", "args": {"task": "write a file", "max_steps": 2}},
            "parent done",
        ],
        delegate_model_client_factory=lambda: FakeModelClient(
            [
                {
                    "name": "write_file",
                    "args": {"path": "child-was-not-allowed.txt", "content": "nope"},
                },
                "child done",
            ]
        ),
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
    with patch.dict(
        os.environ,
        {"HOME": str(tmp_path), "GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
        clear=True,
    ):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(
            run_id="run_001", task_id="task_001", user_request="Mask configured secrets"
        )
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {
            "GITHUB_PAT",
            "GH_PAT",
        }

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
    assert "GITHUB_PAT" not in trace_text
    assert "GH_PAT" not in trace_text
    assert report_text.count("<redacted>") >= 4
