"""Intent classification for context assembly.

The current-turn user message is scanned against four keyword sets:

- ``debug`` — user is reporting an error or asking about failure
- ``recall`` — user is referring to past turns or established knowledge
- ``structural`` — user is asking about project layout or architecture
- ``default`` — anything else

The intent picks a **budget profile** — a token budget for each of the
four injection sources (project_structure, memory_index, recalled_memory,
workspace_state). A debug intent, for example, gets a fatter
workspace_state budget so branch/status/recent-commits reach the model.

**First-match-wins.** Keywords are checked in a fixed priority order
(``debug`` → ``recall`` → ``structural``); the first match short-circuits
and returns. This keeps behavior deterministic when a message contains
multiple triggers ("上次报错了" — both "上次" (recall) and "报错" (debug))
without needing to score or combine profiles.

Matching is case-insensitive substring. No regex, no ML. Callers can
enrich `INTENT_PROFILES` in place if a project needs custom keywords —
the surrounding renderer only depends on the returned ``budget`` dict
shape, not on how it was chosen.
"""

from __future__ import annotations

from typing import NamedTuple


INTENT_PROFILES: dict[str, dict] = {
    "debug": {
        "keywords": ["报错", "error", "traceback", "fail", "not working", "broken", "崩溃"],
        "budget": {
            "workspace_state": 1200,
            "recalled_memory": 600,
            "project_structure": 200,
            "memory_index": 200,
        },
    },
    "recall": {
        "keywords": ["上次", "之前", "记得", "past", "previous", "last time"],
        "budget": {
            "recalled_memory": 1600,
            "memory_index": 800,
            "project_structure": 200,
            "workspace_state": 300,
        },
    },
    "structural": {
        "keywords": ["架构", "结构", "怎么组织", "目录", "layout", "architecture"],
        "budget": {
            "project_structure": 2000,
            "memory_index": 400,
            "recalled_memory": 800,
            "workspace_state": 300,
        },
    },
    "default": {
        "keywords": [],
        "budget": {
            "project_structure": 600,
            "memory_index": 400,
            "recalled_memory": 600,
            "workspace_state": 500,
        },
    },
}

# Priority order for the first-match-wins scan.
_INTENT_ORDER = ("debug", "recall", "structural")


class IntentResult(NamedTuple):
    name: str
    matched_keyword: str
    budget: dict


def classify_intent(user_message):
    """Return an :class:`IntentResult` for ``user_message``.

    Never raises. Empty or falsy input falls through to ``default``.
    The returned ``budget`` is a fresh dict copy — callers can mutate it
    without affecting the shared ``INTENT_PROFILES`` table.
    """
    text = (user_message or "").lower()
    for name in _INTENT_ORDER:
        for kw in INTENT_PROFILES[name]["keywords"]:
            if kw.lower() in text:
                return IntentResult(
                    name=name,
                    matched_keyword=kw,
                    budget=dict(INTENT_PROFILES[name]["budget"]),
                )
    return IntentResult(
        name="default",
        matched_keyword="",
        budget=dict(INTENT_PROFILES["default"]["budget"]),
    )
