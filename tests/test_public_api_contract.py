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
    (tmp_path / ".env").write_text(
        "PICO_API_URL=https://api.deepseek.com/anthropic/v1\n"
        "PICO_DEEPSEEK_API_KEY=test-key\n",
        encoding="utf-8",
    )
    args = build_arg_parser().parse_args([
        "--cwd",
        str(tmp_path),
        "--approval",
        "auto",
    ])

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


def test_internal_model_client_classes_importable_directly():
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
    from pico.providers.fake import FakeModelClient
    from pico.providers.ollama import OllamaModelClient
    from pico.providers.openai_compatible import OpenAICompatibleModelClient
    from pico.providers.openai_chat import OpenAIChatCompletionsModelClient

    for cls in (
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
        OpenAIChatCompletionsModelClient,
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
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["setuptools"] == {
        "packages": [
            "pico",
            "pico._docker_sandbox",
            "pico._sandbox_toolchain",
            "pico.context",
            "pico.evaluation",
            "pico.features",
            "pico.memory",
            "pico.providers",
        ],
        "include-package-data": False,
        "package-data": {
            "pico._docker_sandbox": [
                "image-manifest.json",
                "docker-config/config.json",
            ],
            "pico._sandbox_toolchain": [
                "manifest.json",
                "package.json",
                "package-lock.json",
            ],
        },
    }


def test_docker_sandbox_resources_are_readable():
    import json
    from importlib.resources import files

    root = files("pico._docker_sandbox")
    manifest = json.loads(
        root.joinpath("image-manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["record_type"] == "docker_sandbox_image_set_manifest"
    assert set(manifest["platforms"]) == {"linux/arm64"}
    assert root.joinpath("docker-config", "config.json").read_bytes() == b"{}\n"


def test_sandbox_toolchain_resources_are_readable():
    import json
    from importlib.resources import files

    root = files("pico._sandbox_toolchain")
    manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
    package = json.loads(root.joinpath("package.json").read_text(encoding="utf-8"))
    lock = json.loads(root.joinpath("package-lock.json").read_text(encoding="utf-8"))

    assert manifest["node"]["version"] == "24.18.0"
    assert set(manifest["node"]["artifacts"]) == {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
    }
    assert manifest["srt"]["version"] == "0.0.65"
    assert manifest["f0"] == {
        "status": "rejected",
        "reason_code": "candidate_rejected",
    }
    assert manifest["product"] == {
        "status": "blocked",
        "reason_code": "sandbox_not_released",
    }
    assert package["dependencies"] == {"@anthropic-ai/sandbox-runtime": "0.0.65"}
    assert lock["packages"]["node_modules/@anthropic-ai/sandbox-runtime"]["version"] == "0.0.65"


def test_packaging_exposes_only_pico_cli_script():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    scripts = pyproject_text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    assert scripts.strip() == 'pico = "pico.cli:main"'


def test_fixed_model_defaults_have_one_config_source():
    from pico import config

    assert config.DEFAULT_MODEL == "deepseek-v4-flash"
    assert config.DEFAULT_API_URL == "https://api.deepseek.com/anthropic/v1"
    assert config.API_KEY_ENV_NAME == "PICO_DEEPSEEK_API_KEY"
    assert config.API_URL_ENV_NAME == "PICO_API_URL"
    destinations = {action.dest for action in build_arg_parser()._actions}
    assert {"provider", "profile", "connection", "model", "base_url"}.isdisjoint(
        destinations
    )
