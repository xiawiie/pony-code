from pony.tools.registry import memory_write_intent


def test_memory_write_requires_current_explicit_intent():
    assert memory_write_intent("/remember this")
    assert memory_write_intent("please save this to memory")
    assert not memory_write_intent('explain the phrase "please remember"')
    assert not memory_write_intent("", history=["/remember old"])
