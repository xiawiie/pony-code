import importlib
import sys
import tomllib
from pathlib import Path

import pytest

import pony
from pony import Pony
from pony.cli.app import main
from pony.cli.arguments import build_arg_parser
from pony.cli.assembly import build_agent
from pony.security.trust import ProjectTrustStore
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def test_public_api_exports_current_names_only():
    assert pony.__all__ == ["Pony"]
    assert Pony is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert callable(build_agent)
    assert callable(build_arg_parser)
    assert callable(main)
    for removed in (
        "SessionStore",
        "WorkspaceContext",
        "main",
        "build_agent",
        "build_arg_parser",
    ):
        assert not hasattr(pony, removed)
    assert not hasattr(pony, "MiniAgent")
    assert not hasattr(pony, "FakeModelClient")
    assert not hasattr(pony, "AnthropicMessagesModelClient")
    assert "MiniAgent" not in pony.__all__


def test_build_agent_returns_pony(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=anthropic\n"
        "PONY_API_BASE=https://api.anthropic.com/v1\n"
        "PONY_MODEL=claude-sonnet-4-6\n"
        "PONY_API_KEY=test-key\n",
        encoding="utf-8",
    )
    args = build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--permission-mode",
            "acceptEdits",
        ]
    )

    agent = build_agent(
        args,
        trust_store=ProjectTrustStore(tmp_path / ".pony-home"),
        confirm=lambda _root: True,
    )

    assert isinstance(agent, Pony)


def test_lightweight_package_split_uses_package_paths_without_legacy_shims():
    from benchmarks.evaluation.experiments_recovery import run_context_ablation_v2
    from benchmarks.evaluation.fixed_benchmark import BenchmarkEvaluator
    from benchmarks.support.fake_provider import (
        FakeModelClient as ProviderFakeModelClient,
    )

    assert BenchmarkEvaluator is not None
    assert ProviderFakeModelClient is not None
    assert callable(run_context_ablation_v2)
    for legacy_module in ("models.py", "memory.py", "working_memory.py"):
        assert not (Path("pony") / legacy_module).exists()


def test_memory_feature_exports_exactly_seven_production_helpers():
    import pony.memory.service as memory

    assert memory.__all__ == [
        "canonicalize_path",
        "file_freshness",
        "normalize_file_summaries_dict",
        "set_file_summary_dict",
        "invalidate_file_summary_dict",
        "invalidate_stale_file_summaries_dict",
        "summarize_read_result",
    ]
    assert all(callable(getattr(memory, name)) for name in memory.__all__)
    assert {name for name in vars(memory) if not name.startswith("_")} == set(
        memory.__all__
    )
    for removed in (
        "LayeredMemory",
        "default_memory_state",
        "normalize_memory_state",
        "resolve_workspace_path",
    ):
        assert not hasattr(memory, removed)


def test_internal_model_client_classes_importable_directly():
    from pony.providers.anthropic_messages import AnthropicMessagesModelClient
    from benchmarks.support.fake_provider import FakeModelClient
    from pony.providers.ollama_chat import OllamaChatModelClient
    from pony.providers.openai_responses import OpenAIResponsesModelClient
    from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient

    for cls in (
        FakeModelClient,
        OllamaChatModelClient,
        OpenAIResponsesModelClient,
        OpenAIChatCompletionsModelClient,
        AnthropicMessagesModelClient,
    ):
        assert isinstance(cls, type), f"{cls!r} should be a class"


@pytest.mark.parametrize(
    "module_parts",
    (
        ("pony", "providers", "clients"),
        ("pony", "evaluation", "metrics"),
        ("pony", "evaluation", "metrics_experiments"),
        ("pony", "evaluation", "evaluator"),
    ),
)
def test_removed_facades_cannot_be_imported(module_parts):
    module_name = ".".join(module_parts)
    sys.modules.pop(module_name, None)
    with pytest.raises(ModuleNotFoundError) as caught:
        importlib.import_module(module_name)
    assert module_name.startswith(caught.value.name)


def test_session_store_has_no_runtime_alias():
    from pony.runtime import application as runtime

    assert not hasattr(runtime, "SessionStore")


def test_subpackages_are_markers_without_reexports():
    from benchmarks import evaluation
    from pony import memory, providers, tui

    for package in (evaluation, memory, providers, tui):
        assert not hasattr(package, "__all__")
    assert not hasattr(memory, "VERSION")
    assert not hasattr(memory, "BlockStore")
    assert not hasattr(providers, "FakeModelClient")
    assert not hasattr(evaluation, "BenchmarkEvaluator")


def test_cli_modules_do_not_reexport_test_helpers():
    from pony import cli
    from pony.cli import commands as cli_commands

    assert not hasattr(cli, "HELP_DETAILS")
    for name in (
        "handle_config",
        "handle_doctor",
        "handle_status",
        "handle_memory",
        "handle_checkpoints",
        "handle_runs",
        "handle_sessions",
        "run_agent_once",
        "run_repl",
    ):
        assert not hasattr(cli_commands, name)


def test_packaging_builds_only_the_pony_runtime():
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"] == {
        "requires": ["hatchling>=1.30,<2"],
        "build-backend": "hatchling.build",
    }
    targets = pyproject["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"] == {
        "packages": ["pony"],
        "exclude": ["/pony/sandbox/resources"],
    }
    assert targets["sdist"] == {
        "include": ["/LICENSE", "/README.md", "/pyproject.toml", "/pony"],
        "exclude": ["/pony/sandbox/resources"],
    }


def test_packaging_exposes_only_pony_cli_script():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    scripts = pyproject_text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    assert scripts.strip() == 'pony = "pony.cli.app:main"'


def test_packaging_declares_stable_version_license_and_project_urls():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["version"] in {"1.0.0rc1", "1.0.0"}
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["name"] == "pony-code"
    assert project["urls"]["Source"] == "https://github.com/xiawiie/pony-code"
    assert Path("LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")


def test_provider_defaults_and_generic_env_names_have_one_config_source():
    from pony.config import model as config

    assert config.DEFAULT_PROVIDER == "auto"
    assert config.SUPPORTED_PROVIDERS == (
        "auto",
        "openai",
        "openai-chat",
        "openai-responses",
        "anthropic",
        "ollama",
    )
    assert config.DEFAULT_MODEL == ""
    assert config.DEFAULT_API_BASE == ""
    assert config.PROVIDER_ENV_NAME == "PONY_PROVIDER"
    assert config.MODEL_ENV_NAME == "PONY_MODEL"
    assert config.API_KEY_ENV_NAME == "PONY_API_KEY"
    assert config.API_BASE_ENV_NAME == "PONY_API_BASE"
    destinations = {action.dest for action in build_arg_parser()._actions}
    assert {"provider", "profile", "connection", "model", "base_url"}.isdisjoint(
        destinations
    )
