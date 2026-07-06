import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pico as pico_pkg
from pico.features import memory as memorylib
from pico.runtime import DEFAULT_MAX_NEW_TOKENS, DEFAULT_MAX_STEPS
from pico import (
    FakeModelClient,
    Pico,
    OllamaModelClient,
    SessionStore,
    WorkspaceContext,
    build_welcome,
)


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


def set_raw_file_summary(agent, path, summary):
    memorylib.set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        path,
        summary,
        workspace_root=agent.root,
    )


# =============================================================================
# Agent integration smoke tests
# =============================================================================


def test_pico_constructor_uses_coding_agent_defaults(tmp_path):
    agent = build_agent(tmp_path, [])

    assert agent.max_steps == DEFAULT_MAX_STEPS == 12
    assert agent.max_new_tokens == DEFAULT_MAX_NEW_TOKENS == 2048


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Read the file successfully.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "hello.txt" in agent.session["working_memory"]["recent_files"]
    assert "hello.txt" in agent.session["memory"]["file_summaries"]


def test_agent_updates_task_summary_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>First pass.</final>",
            "<final>Second pass.</final>",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["working_memory"]["task_summary"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["working_memory"]["task_summary"] == "Second request"


def test_agent_stores_file_summaries_without_episodic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"facts.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
            "<final>It is red.</final>",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    assert "facts.txt" in agent.session["working_memory"]["recent_files"]
    assert "deploy key is red" in agent.session["memory"]["file_summaries"]["facts.txt"]["summary"]
    assert "episodic_notes" not in agent.session["memory"]
    assert "notes" not in agent.session["memory"]

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>It is red.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    assert "episodic_notes" not in resumed.session["memory"]
    assert "notes" not in resumed.session["memory"]


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    set_raw_file_summary(agent, "./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    agent._sync_working_memory()
    agent.session_store.save(agent.session)
    assert agent.session["memory"]["file_summaries"]["sample.txt"]["freshness"]

    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert "sample.txt" not in resumed.session["memory"]["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("empty response" in item for item in notices)


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":"bad"}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Recovered after malformed tool output.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("valid <tool> call" in item for item in notices)


def test_agent_accepts_xml_write_file_tool(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer == "Done."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("hi")\n'


def test_retries_do_not_consume_the_whole_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "",
            "<final>Recovered after several retries.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after several retries."


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.session["history"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"inspect README","max_steps":2}}</tool>',
            "<final>Child result.</final>",
            "<final>Parent incorporated the child result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: <tool name="write_file"' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".pico").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".pico" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "2"})

    result = agent.run_tool("list_files", {})

    assert result == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"


def test_repeated_tool_call_rejects_short_alternating_loops(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "README.md", "start": 1, "end": 1},
            "content": "demo",
            "created_at": "2",
        }
    )
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "3"})
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "README.md", "start": 1, "end": 1},
            "content": "demo",
            "created_at": "4",
        }
    )

    result = agent.run_tool("list_files", {})

    assert result == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = tmp_path / "very" / "long" / "path" / "for" / "the" / "pico" / "agent" / "welcome" / "screen"
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "pico" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


# =============================================================================
# Provider client tests
# =============================================================================


# Provider client tests moved to tests/test_provider_clients.py


# =============================================================================
# Build agent / arg parser / packaging tests
# =============================================================================


def test_build_agent_uses_openai_provider_and_model_override(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "openai",
            "model": "override-model",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_BASE": "https://www.right.codes/codex/v1",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_MODEL": "env-model",
        },
        clear=False,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = pico_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["model"] == "override-model"
    assert mock_openai.call_args.kwargs["base_url"] == "https://www.right.codes/codex/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


def test_build_agent_uses_right_codes_shared_key_for_openai_provider(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "openai",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(os.environ, {"PICO_RIGHT_CODES_API_KEY": "sk-right-codes"}, clear=True):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = pico_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["api_key"] == "sk-right-codes"
    assert agent.model_client is fake_client


def test_build_arg_parser_leaves_provider_unset_for_runtime_resolution(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    assert args.provider is None


def test_build_arg_parser_accepts_anthropic_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    assert args.provider == "anthropic"


def test_build_arg_parser_accepts_deepseek_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])

    assert args.provider == "deepseek"


def test_build_agent_uses_project_env_provider_when_cli_omitted(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_PROVIDER=openai",
                "PICO_OPENAI_API_BASE=https://www.right.codes/codex/v1",
                "PICO_OPENAI_API_KEY=sk-project-openai",
                "PICO_OPENAI_MODEL=gpt-5.4",
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(os.environ, {}, clear=True):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.AnthropicCompatibleModelClient",
            side_effect=AssertionError("deepseek client should not be used"),
        ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = pico_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["model"] == "gpt-5.4"
    assert mock_openai.call_args.kwargs["base_url"] == "https://www.right.codes/codex/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-project-openai"
    assert agent.model_client is fake_client


def test_build_agent_prefers_cli_provider_over_project_env_provider(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_PROVIDER=openai",
                "PICO_OPENAI_API_KEY=sk-project-openai",
                "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
                "PICO_DEEPSEEK_MODEL=deepseek-v4-pro",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = pico_pkg.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--provider", "deepseek"]
    )

    with patch.dict(os.environ, {}, clear=True):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-project-deepseek"
    assert agent.model_client is fake_client


def test_build_agent_uses_anthropic_provider_and_openai_key_fallback(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "anthropic",
            "model": "claude-sonnet-4-5-20250929",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-openai-fallback",
        },
        clear=True,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://www.right.codes/claude/v1"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-openai-fallback"
    assert agent.model_client is fake_client


def test_build_agent_uses_anthropic_default_model_when_env_is_missing(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    with patch.dict(
        os.environ,
        {},
        clear=False,
    ):
        os.environ.pop("ANTHROPIC_MODEL", None)
        with patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            pico_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_build_agent_uses_deepseek_provider_and_env_configuration(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
                "PICO_DEEPSEEK_MODEL=deepseek-v4-pro",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "deepseek",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
            "DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "DEEPSEEK_MODEL": "legacy-deepseek-model",
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "OPENAI_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-project-deepseek"
    assert agent.model_client is fake_client


def test_build_agent_uses_deepseek_default_model_when_env_is_missing(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-deepseek"}, clear=True):
        with patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            pico_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"


def test_build_agent_uses_deepseek_provider_by_default(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_BASE": "https://api.deepseek.com/anthropic",
            "DEEPSEEK_API_KEY": "sk-test",
        },
        clear=False,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


# =============================================================================
# Runtime/report/resume tests
# =============================================================================
# Runtime/report/resume tests moved to tests/test_runtime_report.py.


# =============================================================================
# Build agent / arg parser / packaging tests
# =============================================================================


def test_public_api_exports_resolve_through_package_path():
    assert callable(build_welcome)
    assert FakeModelClient is not None
    assert Pico is not None
    assert OllamaModelClient is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert Path(pico_pkg.__file__).as_posix().endswith("/pico/__init__.py")


def test_reviewer_skeleton_docs_exist():
    review_pack = Path("docs/review-pack/README.md")
    architecture = Path("docs/architecture/agent-harness-v1-overview.md")

    assert review_pack.exists()
    assert architecture.exists()

    review_text = review_pack.read_text(encoding="utf-8")
    assert "Project pitch" in review_text
    assert "Architecture map" in review_text
    assert "Benchmark evidence" in review_text
    assert "Sample run artifact list" in review_text

    architecture_text = architecture.read_text(encoding="utf-8")
    assert "Agent Harness v1" in architecture_text
    assert "task state" in architecture_text.lower()
    assert "Run Artifact Terminology" in architecture_text
    assert "`task_state.json`" in architecture_text
    assert "`trace.jsonl`" in architecture_text
    assert "`report.json`" in architecture_text
    assert "not the recovery truth" in architecture_text


def test_package_import_surface_includes_cli_entrypoints():
    assert callable(pico_pkg.main)
    assert callable(pico_pkg.build_agent)
    assert callable(pico_pkg.build_arg_parser)


def test_pico_initializes_recovery_components(tmp_path):
    agent = build_agent(tmp_path, outputs=["<final>ok</final>"])

    assert agent.checkpoint_store.root == tmp_path / ".pico" / "checkpoints"
    assert agent.tool_change_recorder.store is agent.checkpoint_store
    assert agent.recovery_checkpoint_writer.store is agent.checkpoint_store
    assert agent.recovery_manager.store is agent.checkpoint_store


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "pico", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
