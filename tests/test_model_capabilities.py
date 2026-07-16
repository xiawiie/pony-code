import pytest

from pico.model_capabilities import (
    build_model_budget,
    ModelCapabilities,
    resolve_model_capabilities,
    TokenAccounting,
    estimate_text_tokens,
)


@pytest.mark.parametrize(
    ("window", "input_limit", "system_cap", "source_pool"),
    [
        (32_768, 16_384, 6_553, 4_096),
        (128_000, 111_616, 24_576, 16_384),
        (272_000, 255_616, 24_576, 16_384),
        (1_000_000, 983_616, 24_576, 16_384),
    ],
)
def test_budget_scales_across_context_windows(
    window,
    input_limit,
    system_cap,
    source_pool,
):
    capabilities = ModelCapabilities(window, 16_384, "estimate", "config")
    budget = build_model_budget(capabilities)

    assert budget.input_limit == input_limit
    assert budget.system_tools_hard_cap == system_cap
    assert budget.source_pool_tokens == source_pool
    assert budget.compaction_summary_tokens == 13_107
    assert budget.split_turn_summary_tokens == 8_192
    assert budget.branch_summary_tokens == 2_048


def test_output_override_raises_reserve_together():
    capabilities = ModelCapabilities(128_000, 32_768, "estimate", "cli")
    budget = build_model_budget(
        capabilities,
        output_limit=32_768,
        reserve_tokens=16_384,
    )

    assert budget.output_tokens == 32_768
    assert budget.reserve_tokens == 32_768
    assert budget.input_limit == 95_232


def test_invalid_small_window_is_rejected():
    capabilities = ModelCapabilities(30_000, 16_384, "estimate", "config")
    with pytest.raises(ValueError, match="at least 16384 input tokens"):
        build_model_budget(capabilities)


def test_resolution_priority_and_unknown_warning():
    warnings = []
    capabilities = resolve_model_capabilities(
        "unknown-model",
        model_config={"context_window": 64_000, "output_limit": 8_000},
        context_window=96_000,
        warning_sink=warnings.append,
    )
    assert capabilities == ModelCapabilities(
        96_000,
        8_000,
        "provider_usage_or_estimate",
        "cli",
    )
    assert warnings == []

    fallback = resolve_model_capabilities(
        "unknown-model",
        warning_sink=warnings.append,
    )
    assert fallback.context_window == 128_000
    assert fallback.max_output_tokens == 16_384
    assert fallback.source == "fallback"
    assert len(warnings) == 1


def test_cjk_json_and_message_estimates_are_not_ascii_divide_by_four():
    assert estimate_text_tokens("上下文记忆管理") >= 7
    assert estimate_text_tokens("abcdefgh") == 2
    accounting = TokenAccounting()
    assert accounting.count_json({"内容": "上下文"}) > estimate_text_tokens("上下文")
    assert accounting.count_message({"role": "user", "content": "你好"}) > 2


def test_provider_usage_anchor_counts_only_new_tail():
    accounting = TokenAccounting()
    first = accounting.count_request(
        system=[{"text": "system"}],
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
    )
    assert accounting.commit_provider_usage(
        {"input_tokens": 100},
        first.anchor_candidate,
    )
    second = accounting.count_request(
        system=[{"text": "system"}],
        tools=[],
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    )
    assert second.mode == "provider_usage_plus_estimate"
    assert second.total > 100
