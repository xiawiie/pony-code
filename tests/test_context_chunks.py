import pytest

from pony.context.chunks import (
    ContextChunk,
    RequiredContextTooLarge,
    allocate_context_chunks,
)


def _chunk(source, key, tokens, priority, *, required=False, rank=0):
    return ContextChunk(
        source=source,
        key=key,
        text=f"complete-{key}",
        tokens=tokens,
        priority=priority,
        required=required,
        provenance={"rank": rank},
    )


def test_allocator_prioritizes_required_then_p0_and_never_slices_chunks():
    chunks = [
        _chunk("project_structure", "p1", 80, 1),
        _chunk("workspace_state", "p0", 60, 0),
        _chunk("recovery_state", "required", 70, 0, required=True),
    ]

    allocation = allocate_context_chunks(chunks, pool_tokens=140)

    assert [chunk.key for chunk in allocation.selected] == ["required", "p0"]
    assert [(item.chunk.key, item.reason) for item in allocation.dropped] == [
        ("p1", "global_pool")
    ]
    assert all(chunk.text.startswith("complete-") for chunk in allocation.selected)


def test_allocator_enforces_per_source_hard_cap():
    allocation = allocate_context_chunks(
        [
            _chunk("memory_index", "one", 700, 2, rank=0),
            _chunk("memory_index", "two", 700, 2, rank=1),
        ],
        pool_tokens=4_000,
    )

    assert [chunk.key for chunk in allocation.selected] == ["one"]
    assert allocation.dropped[0].reason == "source_cap"


def test_required_chunk_that_cannot_fit_fails_loudly():
    with pytest.raises(RequiredContextTooLarge, match="global_pool"):
        allocate_context_chunks(
            [_chunk("recovery_state", "required", 200, 0, required=True)],
            pool_tokens=100,
        )
