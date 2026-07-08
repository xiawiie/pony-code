"""Stdlib-only frontmatter parser for pico memory notes.

Memory files (``.pico/memory/agent/*.md``, ``.pico/memory/notes/*.md``)
may open with a small YAML-ish header block bounded by ``---`` lines.
The header carries structural metadata (name, type, tags, aliases,
supersedes list, description) that drives retrieval boosts, tombstone
filtering, and the memory index.

Rather than pull in PyYAML for this narrow use, this module parses
a **flat** subset:

- ``key: value`` on its own line
- Only the six recognized ``FRONTMATTER_KEYS`` are captured; unknown
  keys are silently dropped
- List-valued keys (``tags``, ``aliases``, ``supersedes``) accept
  bracket-list syntax ``[a, b, c]``; anything else parses to ``[]``
- No nested dicts, anchors, quoting rules — the intent is that agent-
  or human-written notes stay grep-able and repairable by hand

Malformed input degrades gracefully: if there is no opening ``---``,
no closing ``---``, or no recognized keys, the file is treated as
body-only (``meta = {}``, body = full text). This means a note is
never rejected outright — worst case its metadata is empty and it
falls back to body-only retrieval.
"""

from __future__ import annotations

FRONTMATTER_KEYS = ("name", "type", "description", "tags", "aliases", "supersedes")
_LIST_KEYS = {"tags", "aliases", "supersedes"}


def _parse_list_value(v):
    v = v.strip()
    if not (v.startswith("[") and v.endswith("]")):
        return []
    inner = v[1:-1].strip()
    if not inner:
        return []
    return [x.strip() for x in inner.split(",") if x.strip()]


def parse_frontmatter(text):
    """Return ``(meta_dict, body_str)``.

    ``meta_dict`` is empty when no recognized frontmatter is present.
    ``body_str`` is the remainder after the closing ``---`` line, or
    the full ``text`` when no frontmatter was found.
    """
    if not text.startswith("---\n"):
        return {}, text
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        # Tolerate a trailing "\n---" without a following newline.
        end = rest.rfind("\n---")
        if end == -1 or (end + len("\n---") != len(rest)):
            return {}, text
        block = rest[:end]
        body = ""
    else:
        block = rest[:end]
        body = rest[end + len("\n---\n") :]

    meta = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if key not in FRONTMATTER_KEYS:
            continue
        raw = raw.strip()
        if key in _LIST_KEYS:
            meta[key] = _parse_list_value(raw)
        else:
            meta[key] = raw
    if not meta:
        return {}, text
    return meta, body
