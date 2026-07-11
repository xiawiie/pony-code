import importlib
import sys
from pathlib import Path

import pytest

import pico
from pico import Pico, SessionStore, WorkspaceContext, build_agent, build_arg_parser, build_welcome, main


def test_public_api_exports_current_names_only():
    assert pico.__all__ == [
        "Pico",
        "SessionStore",
        "WorkspaceContext",
        "main",
        "build_agent",
        "build_arg_parser",
        "build_welcome",
    ]
    assert Pico is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert callable(build_agent)
    assert callable(build_arg_parser)
    assert callable(build_welcome)
    assert callable(main)
    assert not hasattr(pico, "MiniAgent")
    assert not hasattr(pico, "FakeModelClient")
    assert not hasattr(pico, "AnthropicCompatibleModelClient")
    assert "MiniAgent" not in pico.__all__


def test_build_agent_returns_pico(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])

    agent = build_agent(args)

    assert isinstance(agent, Pico)


def test_lightweight_package_split_uses_package_paths_without_legacy_shims():
    from pico.evaluation.experiments_recovery import run_context_ablation_v2
    from pico.evaluation.fixed_benchmark import BenchmarkEvaluator
    from pico.providers.fake import FakeModelClient as ProviderFakeModelClient

    assert BenchmarkEvaluator is not None
    assert ProviderFakeModelClient is not None
    assert callable(run_context_ablation_v2)
    for legacy_module in ("models.py", "memory.py", "working_memory.py"):
        assert not (Path("pico") / legacy_module).exists()


def test_memory_feature_exports_exactly_seven_production_helpers():
    from pico.features import memory

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
    assert {
        name for name in vars(memory) if not name.startswith("_")
    } == set(memory.__all__)
    for removed in (
        "LayeredMemory",
        "default_memory_state",
        "normalize_memory_state",
        "resolve_workspace_path",
    ):
        assert not hasattr(memory, removed)


def test_all_four_provider_classes_importable_directly():
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
    from pico.providers.fake import FakeModelClient
    from pico.providers.ollama import OllamaModelClient
    from pico.providers.openai_compatible import OpenAICompatibleModelClient

    for cls in (
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
        AnthropicCompatibleModelClient,
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
    assert caught.value.name == module_name


def test_session_store_has_no_runtime_alias():
    from pico import runtime

    assert not hasattr(runtime, "SessionStore")


def test_subpackages_are_markers_without_reexports():
    from pico import evaluation, memory, providers

    for package in (evaluation, memory, providers):
        assert not hasattr(package, "__all__")
    assert not hasattr(memory, "VERSION")
    assert not hasattr(memory, "BlockStore")
    assert not hasattr(providers, "FakeModelClient")
    assert not hasattr(evaluation, "BenchmarkEvaluator")


def test_cli_modules_do_not_reexport_test_helpers():
    from pico import cli, cli_commands

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


def test_packaging_discovers_pico_subpackages():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.setuptools.packages.find]" in pyproject_text
    assert 'include = ["pico*"]' in pyproject_text


def test_packaging_exposes_only_pico_cli_script():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    scripts = pyproject_text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    assert scripts.strip() == 'pico = "pico.cli:main"'


def test_provider_defaults_have_single_source():
    from pico import cli, cli_diagnostics
    import pico.providers.defaults as defaults

    assert cli.DEFAULT_PROVIDER == defaults.DEFAULT_PROVIDER
    assert cli.DEFAULT_DEEPSEEK_MODEL == defaults.DEFAULT_DEEPSEEK_MODEL
    assert cli.DEFAULT_DEEPSEEK_BASE_URL == defaults.DEFAULT_DEEPSEEK_BASE_URL
    assert cli.PROVIDER_CHOICES == defaults.PROVIDER_CHOICES
    assert cli_diagnostics.DEFAULT_PROVIDER == defaults.DEFAULT_PROVIDER
    assert cli_diagnostics.DEFAULT_MODELS == defaults.DEFAULT_MODELS
    assert cli_diagnostics.DEFAULT_BASE_URLS == defaults.DEFAULT_BASE_URLS

    cli_source = Path("pico/cli.py").read_text(encoding="utf-8")
    diagnostics_source = Path("pico/cli_diagnostics.py").read_text(encoding="utf-8")
    assert 'DEFAULT_PROVIDER = "deepseek"' not in cli_source
    assert 'DEFAULT_PROVIDER = "deepseek"' not in diagnostics_source
