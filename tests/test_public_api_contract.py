from pathlib import Path

import pico
from pico import Pico, SessionStore, WorkspaceContext, build_agent, build_arg_parser, build_welcome, main


def test_public_api_exports_current_names_only():
    assert Pico is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert callable(build_agent)
    assert callable(build_arg_parser)
    assert callable(build_welcome)
    assert callable(main)
    assert not hasattr(pico, "MiniAgent")
    assert "MiniAgent" not in pico.__all__


def test_build_agent_returns_pico(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])

    agent = build_agent(args)

    assert isinstance(agent, Pico)


def test_lightweight_package_split_uses_package_paths_without_legacy_shims():
    from pico.evaluation.evaluator import BenchmarkEvaluator
    from pico.evaluation.metrics import run_context_ablation_v2
    from pico.features.memory import LayeredMemory
    from pico.providers.fake import FakeModelClient as ProviderFakeModelClient

    assert BenchmarkEvaluator is not None
    assert LayeredMemory is not None
    assert ProviderFakeModelClient is not None
    assert callable(run_context_ablation_v2)
    for legacy_module in ("evaluator.py", "metrics.py", "models.py", "memory.py", "working_memory.py"):
        assert not (Path("pico") / legacy_module).exists()


def test_all_four_provider_classes_importable_directly():
    from pico.providers.clients import (
        AnthropicCompatibleModelClient,
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
    )

    for cls in (
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
        AnthropicCompatibleModelClient,
    ):
        assert isinstance(cls, type), f"{cls!r} should be a class"


def test_packaging_discovers_pico_subpackages():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.setuptools.packages.find]" in pyproject_text
    assert 'include = ["pico*"]' in pyproject_text


def test_packaging_exposes_non_conflicting_cli_script():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'pico-cli = "pico.cli:main"' in pyproject_text


def test_provider_defaults_have_single_source():
    from pico import cli, cli_diagnostics
    from pico.providers import defaults

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
