"""Task 9 回归：v1 durable memory 已被完全移除.

任何 durable 相关 API 或常量都不应再存在于 pico 模块中；试图导入必须抛错.
"""

import pytest


def test_durable_memory_store_class_removed():
    import pico.features.memory as memlib
    assert not hasattr(memlib, "DurableMemoryStore"), (
        "DurableMemoryStore should be fully removed (Task 9)"
    )
    assert not hasattr(memlib, "DURABLE_TOPIC_DEFAULTS"), (
        "DURABLE_TOPIC_DEFAULTS should be fully removed (Task 9)"
    )


def test_promote_durable_removed_from_layered_memory():
    from pico.features.memory import LayeredMemory
    mem = LayeredMemory(workspace_root=None)
    assert not hasattr(mem, "promote_durable"), (
        "LayeredMemory.promote_durable should be removed (Task 9)"
    )
    assert not hasattr(mem, "durable_store"), (
        "LayeredMemory.durable_store should be removed (Task 9)"
    )


def test_pico_runtime_has_no_durable_hooks():
    import pico.runtime as runtime
    assert not hasattr(runtime, "DURABLE_MEMORY_INTENT_PATTERN"), (
        "runtime.DURABLE_MEMORY_INTENT_PATTERN should be removed (Task 9)"
    )
    assert not hasattr(runtime, "DURABLE_MEMORY_INTENT_ZH_PATTERN"), (
        "runtime.DURABLE_MEMORY_INTENT_ZH_PATTERN should be removed (Task 9)"
    )
    assert not hasattr(runtime, "DURABLE_MEMORY_LINE_PATTERNS"), (
        "runtime.DURABLE_MEMORY_LINE_PATTERNS should be removed (Task 9)"
    )
    for method in (
        "reject_durable_reason",
        "extract_durable_promotions",
        "promote_durable_memory",
    ):
        assert not hasattr(runtime.Pico, method), (
            f"Pico.{method} should be removed (Task 9)"
        )


def test_build_report_source_has_no_durable_fields():
    """build_report 源码里不应再出现任何 durable_* 键名."""
    import inspect
    import pico.runtime as runtime

    src = inspect.getsource(runtime.Pico.build_report)
    for key in ("durable_promotions", "durable_rejections", "durable_superseded"):
        assert key not in src, (
            f"build_report source still references '{key}' (Task 9)"
        )


def test_agent_loop_source_has_no_promote_durable():
    """agent_loop.py 不再调用 promote_durable_memory."""
    import inspect
    import pico.agent_loop as al

    src = inspect.getsource(al)
    assert "promote_durable_memory" not in src, (
        "agent_loop should no longer call promote_durable_memory (Task 9)"
    )
