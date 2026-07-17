"""Tests for pony.memory.frontmatter.parse_frontmatter — stdlib-only
one-level `key: value` (with bracket-list) parser."""

from pony.memory.frontmatter import parse_frontmatter


def test_valid_frontmatter():
    src = (
        "---\n"
        "name: foo\n"
        "type: feedback\n"
        "description: hi\n"
        "tags: [a, b]\n"
        "aliases: []\n"
        "supersedes: []\n"
        "---\n"
        "body line\n"
    )
    meta, body = parse_frontmatter(src)
    assert meta["name"] == "foo"
    assert meta["type"] == "feedback"
    assert meta["description"] == "hi"
    assert meta["tags"] == ["a", "b"]
    assert meta["aliases"] == []
    assert meta["supersedes"] == []
    assert body == "body line\n"


def test_missing_frontmatter_returns_empty_meta_full_body():
    src = "just body\nno fm"
    meta, body = parse_frontmatter(src)
    assert meta == {}
    assert body == src


def test_malformed_frontmatter_treated_as_body():
    # No closing ---
    src = "---\nnot yaml at all: :::::\n"
    meta, body = parse_frontmatter(src)
    assert meta == {}
    assert body == src


def test_ignores_unknown_keys():
    src = "---\nname: x\nweird_key: whatever\n---\nbody\n"
    meta, body = parse_frontmatter(src)
    assert meta["name"] == "x"
    assert "weird_key" not in meta
    assert body == "body\n"


def test_list_with_trailing_spaces():
    src = "---\nname: x\ntags: [ a , b ,  c ]\n---\nx"
    meta, _body = parse_frontmatter(src)
    assert meta["tags"] == ["a", "b", "c"]


def test_empty_frontmatter_block_returns_empty_meta():
    src = "---\n---\nbody\n"
    meta, body = parse_frontmatter(src)
    # No recognized keys inside; parser bails to no-frontmatter semantics.
    assert meta == {}


def test_body_only_no_trailing_newline():
    src = "---\nname: x\n---\nbody without trailing newline"
    meta, body = parse_frontmatter(src)
    assert meta["name"] == "x"
    assert body == "body without trailing newline"
