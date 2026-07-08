"""Provider-side message utilities.

Provider adapters must never leak pico-internal metadata (``_pico_meta``)
into the wire payload — a future adapter that JSON-dumps messages
directly would otherwise ship stack traces or PII markers to the model.
This helper scrubs the field in a shallow copy; the caller keeps its
own message list untouched.
"""

from __future__ import annotations


def strip_pico_meta(messages):
    """Return a new list of shallow-copied messages with ``_pico_meta`` removed.

    Idempotent: passing an already-stripped list is a no-op. The ``content``
    field is *shared* — nested content-block dicts are trusted (pico never
    writes ``_pico_meta`` inside content blocks).
    """
    out = []
    for msg in messages or []:
        cleaned = {k: v for k, v in msg.items() if k != "_pico_meta"}
        out.append(cleaned)
    return out
