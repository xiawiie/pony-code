from pico.tools import ToolRegistry, memory_write_intent
from pico.tool_executor import PolicyDecision


def test_registry_is_effect_class_source_and_rejects_missing_effect():
    registry = ToolRegistry()
    registry.register("read", schema={}, description="read", effect_class="read_only", runner=lambda args: args)
    assert registry.require("read").effect_class == "read_only"

    try:
        registry.register("bad", schema={}, description="bad", effect_class="", runner=lambda args: args)
    except ValueError as exc:
        assert str(exc) == "invalid_tool_definition"
    else:
        raise AssertionError("missing effect class accepted")


def test_unknown_tool_policy_is_workspace_write_deny():
    decision = PolicyDecision.unknown_tool()
    assert decision.decision == "deny"
    assert decision.reason_code == "unknown_tool"
    assert decision.effect_class == "workspace_write"


def test_memory_write_intent_is_explicit_and_does_not_inherit_or_match_quotes():
    assert memory_write_intent("/remember use uv") is True
    assert memory_write_intent("请记住使用 uv") is True
    assert memory_write_intent("please save this in memory") is True
    assert memory_write_intent("do not remember this") is False
    assert memory_write_intent('explain the phrase "please remember"') is False
    assert memory_write_intent("we need to remember how this works") is False
    assert memory_write_intent("", history=["/remember old fact"]) is False
    assert memory_write_intent("/remember child", delegated=True) is False
