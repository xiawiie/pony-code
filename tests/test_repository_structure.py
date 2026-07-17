import ast
import re
import shlex
import subprocess
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
MAINTAINER_DOCS = {
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "docs/domain-model.md",
    "docs/cli-installation-and-updates.md",
    "docs/architecture.md",
    "docs/security.md",
    "docs/recovery.md",
    "docs/verification.md",
    "docs/memory.md",
    "docs/local-stable-execution.md",
    "docs/adr/0040-docker-filtered-staging.md",
    "docs/adr/0042-sealed-local-authorization.md",
    "docs/context-and-sessions.md",
}
MARKDOWN_FIXTURES = {
    "benchmarks/live_e2e/fixtures/seed_cache_note.md",
    "tests/fixtures/bench_repo_readme/README.md",
}
FORBIDDEN_PREFIXES = (
    ".superpowers/sdd/",
    "benchmarks/results/",
    "docs/review-pack/",
    "docs/superpowers/",
)
FORBIDDEN_MODULES = {
    "benchmarks/evaluation/sandbox_governance.py",
    "pony/providers/clients.py",
    "pony/providers/fake.py",
    "pony/providers/anthropic_compatible.py",
    "pony/providers/openai_compatible.py",
    "pony/providers/openai_chat.py",
    "pony/providers/ollama.py",
    "pony/providers/_shared.py",
    "pony/sandbox/network_control.py",
    "benchmarks/evaluation/metrics.py",
    "benchmarks/evaluation/metrics_experiments.py",
    "benchmarks/evaluation/evaluator.py",
}
ROOT_FILES = {
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "uv.lock",
}
FORBIDDEN_SYMBOLS = {
    "FallbackAdapter",
    "LayeredMemory",
    "SessionMigrationError",
    "build_v2",
    "cli_memory_migrate",
    "complete_v2",
    "find_project_env",
    "load_project_env",
    "stat_all",
    "stream_complete",
    "supports_native_tools",
    "write_agent_topic",
    "_memory_migrate_cmd",
    "_may_import_project_env",
}


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item and (ROOT / item.decode("utf-8")).exists()
    }


def _production_trees(tracked: set[str]):
    for name in sorted(tracked):
        if name.startswith("pony/") and name.endswith(".py"):
            yield (
                name,
                ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name),
            )


def test_tracked_document_surface_is_exact():
    tracked = _tracked_files()
    markdown = {name for name in tracked if name.lower().endswith(".md")}

    assert markdown == MAINTAINER_DOCS | MARKDOWN_FIXTURES
    assert {name for name in tracked if name.startswith("docs/")} == {
        name for name in MAINTAINER_DOCS if name.startswith("docs/")
    }
    for prefix in FORBIDDEN_PREFIXES:
        assert not any(name.startswith(prefix) for name in tracked)


def test_product_root_and_package_surface_are_exact():
    tracked = _tracked_files()
    root_files = {name for name in tracked if "/" not in name}
    assert root_files == ROOT_FILES
    assert {
        name for name in tracked if name.startswith("pony/") and name.count("/") == 1
    } == {
        "pony/__init__.py",
        "pony/__main__.py",
    }
    assert not any(name.startswith("assets/screenshots/") for name in tracked)


def test_agents_instructions_match_the_production_contract():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for name in (
        "PONY_API_BASE",
        "PONY_MODEL",
        "PONY_API_KEY",
    ):
        assert name in text
    for provider in ("anthropic", "openai", "ollama"):
        assert f"`{provider}`" in text
    for package in (
        "agent",
        "cli",
        "context",
        "memory",
        "providers",
        "recovery",
        "sandbox",
        "state",
        "tui",
        "tools",
        "workspace",
    ):
        assert f"`pony/{package}/`" in text
    assert "./scripts/check.sh" in text
    assert "live 未执行" in text
    assert "不回退 Host" in text
    assert "Definition of Done" in text


def test_current_python_and_console_surfaces_are_exact():
    tracked = _tracked_files()
    assert FORBIDDEN_MODULES.isdisjoint(tracked)

    names = set()
    for _, tree in _production_trees(tracked):
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    assert FORBIDDEN_SYMBOLS.isdisjoint(names)

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"pony": "pony.cli.app:main"}

    init_tree = ast.parse((ROOT / "pony/__init__.py").read_text(encoding="utf-8"))
    exports = next(
        ast.literal_eval(node.value)
        for node in init_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    )
    assert exports == ["Pony"]

    for package in (
        "agent",
        "cli",
        "config",
        "context",
        "memory",
        "providers",
        "recovery",
        "runtime",
        "sandbox",
        "security",
        "state",
        "tui",
        "tools",
        "workspace",
    ):
        tree = ast.parse(
            (ROOT / f"pony/{package}/__init__.py").read_text(encoding="utf-8")
        )
        assert all(
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            for node in tree.body
        )


def test_obsolete_provider_and_sandbox_names_are_absent():
    tracked = _tracked_files()
    assert FORBIDDEN_MODULES.isdisjoint(tracked)
    source = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in sorted(tracked)
        if name.endswith(".py")
        and name.startswith(("pony/", "benchmarks/", "scripts/"))
        and "/tests/" not in name
    )
    for obsolete in (
        "build_model_client",
        "_ProviderFailure",
        "image_manifest_digest",
        "image_reference",
        "registry_reference",
        "thinking_disabled",
        "--max-new-tokens",
    ):
        assert obsolete not in source
    assert re.search(r"\b_TARGETS\b", source) is None


def test_current_sources_do_not_read_obsolete_runtime_shapes():
    session_source = (ROOT / "pony/state/session_store.py").read_text(encoding="utf-8")
    checkpoint_source = (ROOT / "pony/state/checkpoint_store.py").read_text(
        encoding="utf-8"
    )
    runtime_source = (ROOT / "pony/runtime/application.py").read_text(encoding="utf-8")
    config_source = (ROOT / "pony/config/model.py").read_text(encoding="utf-8")

    assert '"schema_' + 'version"' not in session_source
    assert '"schema_' + 'version"' not in checkpoint_source
    assert '"hist' + 'ory"' not in session_source
    assert '"prompt_' + 'cache"' not in session_source
    assert '"prompt_' + 'cache"' not in runtime_source
    assert "PONY_" + "RIGHT_CODES_API_KEY" not in config_source
    assert "RIGHT_CODES_" + "API_KEY" not in config_source


def test_public_diagnostics_do_not_import_superseded_srt_owners():
    tree = ast.parse(
        (ROOT / "pony/cli/diagnostics.py").read_text(encoding="utf-8"),
        filename="pony/cli/diagnostics.py",
    )
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1
    }

    assert imported.isdisjoint(
        {
            "sandbox_lifecycle",
            "sandbox_linux",
            "sandbox_macos",
            "sandbox_toolchain",
        }
    )


def test_all_provider_methods_use_the_structured_completion_surface():
    expected = {
        "pony/providers/anthropic_messages.py": {
            "AnthropicMessagesModelClient": {"complete"},
        },
        "benchmarks/support/fake_provider.py": {"FakeModelClient": {"complete"}},
        "pony/providers/openai_responses.py": {
            "OpenAIResponsesModelClient": {"complete"},
        },
        "pony/providers/openai_chat_completions.py": {
            "OpenAIChatCompletionsModelClient": {"complete"},
        },
        "pony/providers/ollama_chat.py": {"OllamaChatModelClient": {"complete"}},
    }
    for filename, classes in expected.items():
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        methods = {
            node.name: {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in {"complete", "complete_text"}
            }
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        for class_name, required in classes.items():
            assert methods[class_name] == required


def test_code_imports_real_modules_not_empty_package_facades():
    tracked = _tracked_files()
    facade_modules = {"pony.providers", "pony.memory"}
    offenders = []
    for name in sorted(tracked):
        if not name.endswith(".py") or not name.startswith(
            ("pony/", "tests/", "benchmarks/", "scripts/")
        ):
            continue
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module in facade_modules
            ):
                offenders.append((name, node.lineno, node.module))
    assert offenders == []


def test_maintainer_doc_links_and_cli_examples_resolve():
    tracked = _tracked_files()
    link_pattern = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
    allowed_commands = {
        "--approval",
        "--cwd",
        "--format",
        "--help",
        "--sandbox",
        "--version",
        "checkpoints",
        "config",
        "doctor",
        "help",
        "init",
        "memory",
        "migrate",
        "repl",
        "run",
        "runs",
        "sandbox",
        "session",
        "sessions",
        "status",
    }
    for name in sorted(MAINTAINER_DOCS):
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(text):
            target = raw_target.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.is_relative_to(ROOT), (name, raw_target)
            relative = resolved.relative_to(ROOT).as_posix()
            assert relative in tracked, (name, raw_target)

        in_fence = False
        for line in text.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            for match in re.finditer(r"(?:^|\|\s*|uv run )pony\s+([^\s]+)", line):
                token = shlex.split(match.group(1))[0]
                assert token in allowed_commands, (name, line)


def test_gitignore_allows_current_docs_and_ignores_local_drafts():
    for name in MAINTAINER_DOCS:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", name],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, name
    for name in ("docs/local-draft.md", "docs/superpowers/local.md"):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", name],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, name

    for name in (
        ".pony/memory/notes/team.md",
        ".pony/memory/notes/nested/decision.md",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", name],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, name
    for name in (
        ".pony/memory/agent_notes.md",
        ".pony/memory/notes/private.txt",
        ".pony/runs/run.json",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", name],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, name
