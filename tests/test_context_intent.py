"""Tests for pico.context.intent — regex-based intent classifier with
first-match-wins priority (debug > recall > structural > default)."""

from pico.context.intent import INTENT_PROFILES, classify_intent


def test_default_when_no_match():
    r = classify_intent("random neutral message")
    assert r.name == "default"
    assert r.matched_keyword == ""
    assert r.budget == INTENT_PROFILES["default"]["budget"]


def test_debug_wins_over_recall_when_both_present():
    # first-match-wins priority: debug > recall > structural
    # "上次" is a recall keyword; "报错" is a debug keyword — debug wins.
    r = classify_intent("上次报错了")
    assert r.name == "debug"


def test_recall_keyword_hit():
    r = classify_intent("上次讨论过什么？")
    assert r.name == "recall"
    assert r.matched_keyword == "上次"


def test_structural_keyword_hit():
    r = classify_intent("讲讲这个项目的架构")
    assert r.name == "structural"


def test_case_insensitive():
    r = classify_intent("what is the ARCHITECTURE?")
    assert r.name == "structural"


def test_budget_dict_has_five_sources():
    r = classify_intent("random")
    assert set(r.budget.keys()) == {
        "project_structure",
        "memory_index",
        "recalled_memory",
        "workspace_state",
        "checkpoint",
    }


def test_budget_dict_is_copy_not_shared():
    r = classify_intent("random")
    r.budget["project_structure"] = 999
    # Second call should return the original default, not the mutated dict.
    r2 = classify_intent("random")
    assert r2.budget["project_structure"] != 999
