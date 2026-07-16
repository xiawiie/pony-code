"""New pico.toml model, Context, compaction, and tool-result budgets."""

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.agent.loop import _prepare_tool_result
from pico.config import load_pico_toml
from pico.context.renderer import render_current_user_message
from pico.agent.context_manager import SystemContextTooLarge
from pico.agent.model_capabilities import TokenAccounting
from pico.providers.fake import FakeModelClient


def _config(root):
    return load_pico_toml(root)


def test_model_and_context_defaults(tmp_path):
    config = _config(tmp_path)

    assert config["model"] == {
        "context_window": 128_000,
        "output_limit": 16_384,
    }
    assert config["context"] == {
        "system_tools_hard_cap": 24_576,
        "source_pool_tokens": 16_384,
        "compaction": {
            "enabled": True,
            "reserve_tokens": 16_384,
            "keep_recent_tokens": 20_000,
        },
        "tool_results": {"inline_tokens": 4_096, "digest_tokens": 512},
    }


def test_new_budget_overrides(tmp_path):
    (tmp_path / "pico.toml").write_text(
        """
[model]
context_window = 272000
output_limit = 24000

[context]
system_tools_hard_cap = 30000
source_pool_tokens = 18000

[context.compaction]
enabled = false
reserve_tokens = 32000
keep_recent_tokens = 24000

[context.tool_results]
inline_tokens = 2048
digest_tokens = 384
""",
        encoding="utf-8",
    )

    config = _config(tmp_path)

    assert config["model"] == {
        "context_window": 272_000,
        "output_limit": 24_000,
    }
    assert config["context"]["system_tools_hard_cap"] == 30_000
    assert config["context"]["source_pool_tokens"] == 18_000
    assert config["context"]["compaction"] == {
        "enabled": False,
        "reserve_tokens": 32_000,
        "keep_recent_tokens": 24_000,
    }
    assert config["context"]["tool_results"] == {
        "inline_tokens": 2_048,
        "digest_tokens": 384,
    }


def test_removed_context_fields_warn_and_do_not_survive(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        """
[context]
history_soft_cap = 12345
history_floor_messages = 4
injection_budget_ratio = 0.2
""",
        encoding="utf-8",
    )

    context = _config(tmp_path)["context"]
    warnings = capsys.readouterr().err

    assert "history_soft_cap" not in context
    assert "history_floor_messages" not in context
    assert "injection_budget_ratio" not in context
    assert "automatic compaction" in warnings
    assert "source_pool_tokens" in warnings


def test_legacy_total_budget_maps_to_model_context_window(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        "[context]\ntotal_budget_hard_cap = 50000\n",
        encoding="utf-8",
    )

    config = _config(tmp_path)

    assert config["model"]["context_window"] == 50_000
    assert "deprecated" in capsys.readouterr().err


def test_explicit_model_context_wins_over_legacy_total(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[model]\ncontext_window = 128000\n"
        "[context]\ntotal_budget_hard_cap = 50000\n",
        encoding="utf-8",
    )

    assert _config(tmp_path)["model"]["context_window"] == 128_000


def test_invalid_budget_types_fall_back_independently(tmp_path):
    (tmp_path / "pico.toml").write_text(
        """
[model]
context_window = "large"
output_limit = -1
[context]
source_pool_tokens = true
[context.tool_results]
inline_tokens = 0
digest_tokens = 256
""",
        encoding="utf-8",
    )

    config = _config(tmp_path)

    assert config["model"] == {
        "context_window": 128_000,
        "output_limit": 16_384,
    }
    assert config["context"]["source_pool_tokens"] == 16_384
    assert config["context"]["tool_results"] == {
        "inline_tokens": 4_096,
        "digest_tokens": 256,
    }


def test_prepare_tool_result_uses_token_limits(tmp_path):
    from types import SimpleNamespace

    agent = SimpleNamespace(
        current_run_dir=tmp_path / ".pico" / "runs" / "r1",
        context_config={
            "tool_results": {"inline_tokens": 20, "digest_tokens": 64}
        },
        token_accounting=TokenAccounting(),
        redact_text=str,
    )
    agent.current_run_dir.mkdir(parents=True)

    content, metadata = _prepare_tool_result(
        agent,
        content="x" * 100,
        tool_name="read_file",
        tool_args={"path": "a.py"},
    )

    assert "[digest]" in content
    assert metadata["digest_applied"] is True
    assert agent.token_accounting.count_text(content) <= 64


def test_system_tools_hard_cap_fails_loudly_instead_of_truncating(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nsystem_tools_hard_cap = 100\n",
        encoding="utf-8",
    )
    workspace = WorkspaceContext.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        workspace,
        SessionStore(tmp_path / ".pico" / "sessions"),
    )
    agent.session["messages"].append(
        {"role": "user", "content": "hi", "_pico_meta": {"created_at": "t"}}
    )
    snapshot, telemetry = render_current_user_message(agent, "hi")

    with pytest.raises(SystemContextTooLarge):
        agent.context_manager.build_request(
            injection_snapshot=snapshot,
            injection_telemetry=telemetry,
            preflight_metadata={},
        )
