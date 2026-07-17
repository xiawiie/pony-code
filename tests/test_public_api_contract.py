import importlib
import sys
import tomllib
from pathlib import Path

import pytest

import pico
from pico import Pico
from pico.cli.app import main
from pico.cli.arguments import build_arg_parser
from pico.cli.assembly import build_agent
from pico.state.session_store import SessionStore
from pico.workspace.context import WorkspaceContext


def test_public_api_exports_current_names_only():
    assert pico.__all__ == ["Pico"]
    assert Pico is not None
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
        assert not hasattr(pico, removed)
    assert not hasattr(pico, "MiniAgent")
    assert not hasattr(pico, "FakeModelClient")
    assert not hasattr(pico, "AnthropicMessagesModelClient")
    assert "MiniAgent" not in pico.__all__


def test_build_agent_returns_pico(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PICO_API_BASE=https://api.anthropic.com/v1\n"
        "PICO_MODEL=claude-sonnet-4-6\n"
        "PICO_API_KEY=test-key\n",
        encoding="utf-8",
    )
    args = build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--approval",
            "auto",
        ]
    )

    agent = build_agent(args)

    assert isinstance(agent, Pico)


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
        assert not (Path("pico") / legacy_module).exists()


def test_memory_feature_exports_exactly_seven_production_helpers():
    import pico.memory.service as memory

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
    from pico.providers.anthropic_messages import AnthropicMessagesModelClient
    from benchmarks.support.fake_provider import FakeModelClient
    from pico.providers.ollama_chat import OllamaChatModelClient
    from pico.providers.openai_responses import OpenAIResponsesModelClient
    from pico.providers.openai_chat_completions import OpenAIChatCompletionsModelClient

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
        ("pico", "providers", "clients"),
        ("pico", "evaluation", "metrics"),
        ("pico", "evaluation", "metrics_experiments"),
        ("pico", "evaluation", "evaluator"),
    ),
)
def test_removed_facades_cannot_be_imported(module_parts):
    module_name = ".".join(module_parts)
    sys.modules.pop(module_name, None)
    with pytest.raises(ModuleNotFoundError) as caught:
        importlib.import_module(module_name)
    assert module_name.startswith(caught.value.name)


def test_session_store_has_no_runtime_alias():
    from pico.runtime import application as runtime

    assert not hasattr(runtime, "SessionStore")


def test_subpackages_are_markers_without_reexports():
    from benchmarks import evaluation
    from pico import memory, providers, tui

    for package in (evaluation, memory, providers, tui):
        assert not hasattr(package, "__all__")
    assert not hasattr(memory, "VERSION")
    assert not hasattr(memory, "BlockStore")
    assert not hasattr(providers, "FakeModelClient")
    assert not hasattr(evaluation, "BenchmarkEvaluator")


def test_cli_modules_do_not_reexport_test_helpers():
    from pico import cli
    from pico.cli import commands as cli_commands

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


def test_packaging_builds_only_the_pico_runtime():
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"] == {
        "requires": ["hatchling>=1.30,<2"],
        "build-backend": "hatchling.build",
    }
    targets = pyproject["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"] == {"packages": ["pico"]}
    assert targets["sdist"] == {
        "include": ["/LICENSE", "/README.md", "/pyproject.toml", "/pico"]
    }


def test_docker_sandbox_resources_are_readable():
    import json
    from importlib.resources import files

    root = files("pico.sandbox.resources")
    manifest = json.loads(
        root.joinpath("image-manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["record_type"] == "docker_sandbox_image_set_manifest"
    assert manifest["format_version"] == 3
    assert set(manifest) == {
        "record_type",
        "format_version",
        "policy_digest",
        "user",
        "working_dir",
        "env",
        "tool_paths",
        "platforms",
    }
    assert set(manifest["platforms"]) == {"linux/arm64"}
    assert set(manifest["platforms"]["linux/arm64"]) == {
        "image_digest",
        "image_id",
    }
    assert root.joinpath("docker-config", "config.json").read_bytes() == b"{}\n"


def test_packaging_exposes_only_pico_cli_script():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    scripts = pyproject_text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    assert scripts.strip() == 'pico = "pico.cli.app:main"'


def test_packaging_declares_stable_version_license_and_project_urls():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["version"] in {"1.0.0rc1", "1.0.0"}
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["urls"]["Source"] == "https://github.com/xiawiie/pico"
    assert Path("LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")


def test_provider_defaults_and_generic_env_names_have_one_config_source():
    from pico.config import model as config

    assert config.DEFAULT_PROVIDER == "anthropic"
    assert config.SUPPORTED_PROVIDERS == ("anthropic", "openai", "ollama")
    assert config.DEFAULT_MODEL == "claude-sonnet-4-6"
    assert config.DEFAULT_API_BASE == "https://api.anthropic.com/v1"
    assert config.MODEL_ENV_NAME == "PICO_MODEL"
    assert config.API_KEY_ENV_NAME == "PICO_API_KEY"
    assert config.API_BASE_ENV_NAME == "PICO_API_BASE"
    destinations = {action.dest for action in build_arg_parser()._actions}
    assert {"provider", "profile", "connection", "model", "base_url"}.isdisjoint(
        destinations
    )
