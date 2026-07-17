from pico.tools.executor import PolicyDecision


def test_policy_decision_is_explicit_and_fail_closed():
    decision = PolicyDecision.unknown_tool().to_dict()
    assert decision["decision"] == "deny"
    assert decision["reason_code"] == "unknown_tool"
    assert decision["effect_class"] == "workspace_write"
