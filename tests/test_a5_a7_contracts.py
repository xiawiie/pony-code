from pony.tools.registry import memory_write_intent
from pony.tools.executor import PolicyDecision


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
