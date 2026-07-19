import os

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.context.renderer import render_current_user_message
from pony.context.skills import (
    MAX_PROJECT_SKILLS,
    MAX_SKILL_FILE_BYTES,
    discover_project_skills,
)
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _skill(root, name="review", *, body="Inspect first.", description="Review safely."):
    path = root / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def _catalog(root, **kwargs):
    return discover_project_skills(
        root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        **kwargs,
    )


def _agent(root, outputs=()):
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def test_discovers_only_strict_claude_project_skill_layout(tmp_path):
    _skill(tmp_path, "review")
    _skill(tmp_path, "design", body="Think before coding.")
    (tmp_path / ".agents" / "skills" / "other").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "other" / "SKILL.md").write_text(
        "not a project skill", encoding="utf-8"
    )

    catalog = _catalog(tmp_path)

    assert tuple(skill.name for skill in catalog.skills) == ("design", "review")
    assert catalog.get("review").instructions == "Inspect first."


@pytest.mark.parametrize(
    "setup",
    (
        lambda root: _skill(root, body=""),
        lambda root: (root / ".claude" / "skills" / "review").mkdir(parents=True),
        lambda root: _skill(root, description=""),
        lambda root: _skill(root, name="Review"),
    ),
)
def test_malformed_or_incomplete_catalog_fails_closed(tmp_path, setup):
    setup(tmp_path)

    assert _catalog(tmp_path).skills == ()


def test_rejects_symlink_hardlink_and_oversized_skill_documents(tmp_path):
    outside = tmp_path.parent / "outside-skill.md"
    outside.write_text("outside canary\n", encoding="utf-8")
    linked = tmp_path / ".claude" / "skills" / "review" / "SKILL.md"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    assert _catalog(tmp_path).skills == ()

    linked.unlink()
    os.link(outside, linked)
    assert _catalog(tmp_path).skills == ()

    linked.unlink()
    _skill(tmp_path, body="x" * MAX_SKILL_FILE_BYTES)
    assert _catalog(tmp_path).skills == ()


def test_invalid_entry_and_limit_fail_closed(tmp_path):
    _skill(tmp_path, "review")
    (tmp_path / ".claude" / "skills" / "README").write_text(
        "not a directory", encoding="utf-8"
    )
    assert _catalog(tmp_path).skills == ()

    (tmp_path / ".claude" / "skills" / "README").unlink()
    _skill(tmp_path, "plan")
    assert _catalog(tmp_path, reserved_names=("/plan",)).skills == ()

    (tmp_path / ".claude" / "skills" / "plan" / "SKILL.md").unlink()
    (tmp_path / ".claude" / "skills" / "plan").rmdir()
    for number in range(MAX_PROJECT_SKILLS):
        _skill(tmp_path, f"skill-{number}")
    assert _catalog(tmp_path).skills == ()


def test_skill_secret_content_is_not_loaded(tmp_path):
    secret = "github_pat_" + "A" * 40
    _skill(tmp_path, body=f"Use {secret}.")

    assert _catalog(tmp_path).skills == ()


def test_skill_known_runtime_secret_is_not_loaded(tmp_path):
    secret = "known-runtime-secret"
    _skill(tmp_path, body=f"Use {secret}.")

    assert _catalog(tmp_path, env={"PONY_API_KEY": secret}).skills == ()


def test_skill_catalog_rejects_skill_root_identity_drift(tmp_path, monkeypatch):
    from pony.context import skills as skills_module

    _skill(tmp_path)
    skill_root = tmp_path / ".claude" / "skills"
    real_identity = skills_module.private_directory_identity
    calls = 0

    def drifted_identity(path):
        nonlocal calls
        if path == skill_root:
            calls += 1
            return (1, calls)
        return real_identity(path)

    monkeypatch.setattr(skills_module, "private_directory_identity", drifted_identity)

    assert _catalog(tmp_path).skills == ()


def test_skill_is_injected_only_for_explicit_skill_turn(tmp_path):
    _skill(tmp_path, body="Review the diff before edits.")
    agent = _agent(tmp_path)
    skill = agent.project_skill("review")

    plain, _ = render_current_user_message(agent, "hello")
    assert "Review the diff" not in plain

    agent.active_skill = skill
    try:
        rendered, telemetry = render_current_user_message(agent, "inspect")
    finally:
        agent.active_skill = None

    assert "<pony:active_skill>" in rendered
    assert "Review the diff before edits." in rendered
    assert telemetry["context_source_allocator"]["selected_chunks"] >= 1


def test_oversized_active_skill_fails_before_provider_request(tmp_path):
    _skill(tmp_path, body="x" * (MAX_SKILL_FILE_BYTES - 128))
    agent = _agent(tmp_path, outputs=("must not run",))
    skill = agent.project_skill("review")
    agent.model_budget = agent.model_budget.__class__(
        **{**agent.model_budget.__dict__, "source_pool_tokens": 100}
    )

    with pytest.raises(RuntimeError, match="required context chunk active_skill"):
        agent.ask("inspect", skill=skill)

    assert agent.model_client.requests == []


def test_repl_skill_routes_prompt_and_keeps_skill_out_of_session(tmp_path, capsys):
    from pony.cli.start import _process_repl_input

    _skill(tmp_path, body="Return REVIEWED after inspecting.")
    agent = _agent(tmp_path, outputs=("REVIEWED",))

    _process_repl_input(agent, "/review inspect this")

    assert "REVIEWED" in capsys.readouterr().out
    assert agent.active_skill is None
    assert all(
        "Return REVIEWED" not in str(message["content"])
        for message in agent.session["messages"]
    )


def test_skill_appears_in_shared_help_and_tui_completion(tmp_path):
    from prompt_toolkit.document import Document

    from pony.cli.help import render_help_details
    from pony.tui.app import SlashCommandCompleter

    _skill(tmp_path)
    agent = _agent(tmp_path)

    assert "/review [prompt]" in render_help_details(agent)
    completions = list(
        SlashCommandCompleter(agent).get_completions(Document("/rev"), None)
    )
    assert [item.text for item in completions] == ["/review"]
