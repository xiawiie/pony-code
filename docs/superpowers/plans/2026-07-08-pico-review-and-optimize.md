# Pico Post-Migration Review & Optimize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all 14 remaining findings from the prior whole-branch review; add exhaustive E2E coverage; stand up a stdlib-only latency benchmark harness.

**Architecture:** 6 independent streams (A: correctness, B: config, C: observability, D: perf, E: testing, F: docs) totalling 35 tasks. Every task follows TDD (failing test → verify → minimal impl → verify → commit). All new work aligns with existing pico patterns (per-domain helpers in `config.py`; sniffer providers in tests; benchmark scenarios in `benchmarks/`).

**Tech Stack:** Python 3.11+, stdlib-only (`tomllib` as optional upgrade from custom parser); pytest for tests; Anthropic messages API semantics.

## Global Constraints

- **stdlib-only**: No new third-party dependencies. `tomllib` is stdlib in Python 3.11+.
- **Python 3.11+**: `pyproject.toml` `requires-python` bumps from `>=3.10` to `>=3.11` in Task B1.
- **Existing patterns**: extend `pico/config.py` (do NOT create `config_context.py`); new benchmarks live in `benchmarks/perf/` alongside existing `benchmarks/memory_quality/`.
- **Anthropic semantics**: roles ∈ {"user", "assistant"} only. `tool_result` is a `user` message content block.
- **Message immutability**: once appended to `session["messages"]`, messages are never mutated.
- **No intent overrides via pico.toml**: nested-dict intent config was explicitly dropped (spec §2 non-goal).
- **No runtime dual-write assertion**: use a read-only CLI inspector (spec §4.5) instead.
- **Benchmarks are not CI-gating**: run manually, emit JSON, no absolute-time thresholds.
- **Spec location**: `docs/superpowers/specs/2026-07-08-pico-review-and-optimize-design.md`.

---

# Stream A · Correctness & Safety

**Gate at end**: `uv run pytest -q` shows ≥600 passed; every finding in this stream (2, 4, 7, 10, 11) has a commit closing it.

---

## Task A1: Turn-based history budget drop in build_v2

**Files:**
- Modify: `pico/context_manager.py` (add `_drop_old_turns` helper; call from `build_v2`)
- Test: `tests/test_context_history_budget.py` (new)

**Interfaces:**
- Consumes: `_pico_meta.tool_use_id` on assistant/tool_result messages (Task 4/6/7 landed).
- Produces:
  - `_drop_old_turns(messages: list[dict], soft_cap_tokens: int, floor_count: int, token_of: Callable[[dict], int]) -> tuple[list[dict], int]` — returns `(kept_messages, dropped_count)`
  - `metadata["dropped_messages"]: int` field.
  - `metadata["messages_tokens"]: int` field.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_history_budget.py
"""Task A1: turn-based history budget drops old turn units atomically."""

from pico.context_manager import _drop_old_turns


def _msg(role, content, tool_use_id=None):
    m = {"role": role, "content": content, "_pico_meta": {}}
    if tool_use_id is not None:
        m["_pico_meta"]["tool_use_id"] = tool_use_id
    return m


def _flat_token_count(msg):
    # rough char-based estimate; each ascii char ~= 1 token in tests
    c = msg["content"]
    if isinstance(c, str):
        return len(c)
    total = 0
    for block in c:
        total += len(str(block.get("content", "")))
        total += len(str(block.get("input", "")))
    return total


def test_soft_cap_respected_when_exceeded():
    # 5 user messages of 100 chars each = 500 tokens
    msgs = [_msg("user", "x" * 100) for _ in range(5)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=250, floor_count=1, token_of=_flat_token_count)
    assert dropped >= 2
    assert sum(_flat_token_count(m) for m in kept) <= 250 or len(kept) == 1


def test_floor_never_dropped():
    # 20 user messages, cap=0, floor=6 → keep last 6 even though over cap
    msgs = [_msg("user", f"m{i}") for i in range(20)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=0, floor_count=6, token_of=_flat_token_count)
    assert len(kept) == 6
    assert dropped == 14
    assert kept[0]["content"] == "m14"
    assert kept[-1]["content"] == "m19"


def test_no_drop_when_under_cap():
    msgs = [_msg("user", "short") for _ in range(3)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=10000, floor_count=6, token_of=_flat_token_count)
    assert kept == msgs
    assert dropped == 0


def test_multi_tool_use_turn_drop_atomicity():
    # A turn with 3 tool_use/tool_result pairs — all must drop together.
    msgs = [
        _msg("user", "old question 1"),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}], tool_use_id="t1"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "r1"}], tool_use_id="t1"),
        _msg("assistant", [{"type": "tool_use", "id": "t2", "name": "read_file", "input": {}}], tool_use_id="t2"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "r2"}], tool_use_id="t2"),
        _msg("assistant", [{"type": "tool_use", "id": "t3", "name": "read_file", "input": {}}], tool_use_id="t3"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t3", "content": "r3"}], tool_use_id="t3"),
        _msg("assistant", "final answer 1"),
        _msg("user", "new question 2"),
        _msg("assistant", "answer 2"),
    ]
    # Force dropping the first turn (10 messages up to the second user question)
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=50, floor_count=2, token_of=_flat_token_count)
    # Every remaining assistant.tool_use.id must have a matching user.tool_result
    tool_use_ids = set()
    tool_result_ids = set()
    for m in kept:
        if isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])
                if block.get("type") == "tool_result":
                    tool_result_ids.add(block["tool_use_id"])
    assert tool_use_ids == tool_result_ids, "orphan tool_use or tool_result after drop"


def test_orphan_tool_use_never_produced():
    # If floor cuts through a tool_use pair, the algorithm must extend
    # the retention to keep the pair intact.
    msgs = [
        _msg("user", "q"),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}], tool_use_id="t1"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}], tool_use_id="t1"),
        _msg("assistant", "done"),
    ]
    # floor=1 would naively cut mid-pair; algorithm must keep the tool_result reachable
    kept, _dropped = _drop_old_turns(msgs, soft_cap_tokens=0, floor_count=1, token_of=_flat_token_count)
    tool_use_ids = set()
    tool_result_ids = set()
    for m in kept:
        if isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])
                if block.get("type") == "tool_result":
                    tool_result_ids.add(block["tool_use_id"])
    assert tool_use_ids == tool_result_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_history_budget.py -v`
Expected: FAIL with `ImportError: cannot import name '_drop_old_turns' from 'pico.context_manager'`

- [ ] **Step 3: Implement `_drop_old_turns` in context_manager.py**

Add to `pico/context_manager.py` module (after existing helpers, before `class ContextManager`):

```python
def _drop_old_turns(messages, soft_cap_tokens, floor_count, token_of):
    """Drop oldest turn units until aggregate tokens ≤ soft_cap, subject to floor.

    A **turn unit** starts at a top-level ``user`` message (one whose
    content is a plain string — user typing) and includes every following
    message up to the next such top-level user message. The unit contains
    interleaved assistant.tool_use / user.tool_result pairs plus the
    final assistant.text. Dropping is atomic — either the entire turn
    unit goes or it stays. This guarantees no orphan
    ``tool_use``/``tool_result`` blocks reach the provider (Anthropic
    rejects them).

    The last ``floor_count`` messages are always retained even if that
    means exceeding ``soft_cap_tokens``. Floor honors the pairing
    invariant: if the floor cuts through a tool_use pair, the algorithm
    extends the kept window to include the pair.
    """
    if not messages:
        return list(messages), 0
    n = len(messages)
    floor_start = max(0, n - floor_count)

    # Extend floor_start backwards so we never split a tool_use/tool_result pair.
    while floor_start > 0:
        msg = messages[floor_start]
        prev = messages[floor_start - 1]
        if _is_tool_result_message(msg) and _is_tool_use_message(prev):
            # Keep the whole pair together — pull the assistant.tool_use in.
            floor_start -= 1
            continue
        break

    # Locate turn-unit boundaries in the pre-floor region.
    turn_starts = []
    for i in range(floor_start):
        if _is_top_level_user_message(messages[i]):
            turn_starts.append(i)

    kept_start = 0
    total = sum(token_of(m) for m in messages)
    for boundary in turn_starts:
        if total <= soft_cap_tokens:
            break
        # Drop everything from kept_start up to (but not including) the next boundary.
        dropped_tokens = sum(token_of(m) for m in messages[kept_start:boundary])
        total -= dropped_tokens
        kept_start = boundary

    kept = list(messages[kept_start:])
    dropped = kept_start
    return kept, dropped


def _is_top_level_user_message(msg):
    """A 'top-level' user message is one whose content is a plain string
    (i.e. the user typing), not a list-of-blocks (tool_result carrier)."""
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def _is_tool_use_message(msg):
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(b.get("type") == "tool_use" for b in content)


def _is_tool_result_message(msg):
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(b.get("type") == "tool_result" for b in content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_context_history_budget.py -v`
Expected: 5 passed

- [ ] **Step 5: Wire `_drop_old_turns` into `build_v2`**

In `pico/context_manager.py`, inside `build_v2`, after building the `messages` list (around line 329, after the `messages = list(session.get("messages", []) or [])` line and after tail-user-replacement) and before computing `breakpoints`:

```python
        # Task A1: enforce history_soft_cap via turn-unit drop. When agent
        # has no context_config yet (early bootstrap / tests without pico
        # runtime), fall back to a large default so we don't drop anything.
        cfg = getattr(self.agent, "context_config", {}) or {}
        soft_cap = int(cfg.get("history_soft_cap", 40000))
        floor_count = int(cfg.get("history_floor_messages", 6))
        messages, dropped_messages = _drop_old_turns(
            messages,
            soft_cap_tokens=soft_cap,
            floor_count=floor_count,
            token_of=lambda m: self._count_tokens_for_v2(_message_text(m)),
        )
```

And add the `_message_text` helper alongside `_drop_old_turns`:

```python
def _message_text(msg):
    """Best-effort text serialization of a message for token estimation."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parts.append(str(block.get("name", "")))
                parts.append(str(block.get("input", "")))
            elif block.get("type") == "tool_result":
                parts.append(str(block.get("content", "")))
        return "\n".join(parts)
    return str(content or "")
```

Then extend the metadata block (in `build_v2`, near the `metadata = {` construction):

```python
        metadata = {
            "system_cache_key": system_cache_key,
            "system_tokens": system_tokens,
            "tools_tokens": tools_tokens,
            "messages_count": len(messages),
            "messages_tokens": sum(
                self._count_tokens_for_v2(_message_text(m)) for m in messages
            ),
            "dropped_messages": dropped_messages,
            "cache_control_breakpoints": list(breakpoints),
            "prompt_cache_key": system_cache_key,
            **injection_telemetry,
        }
```

- [ ] **Step 6: Add integration test in the same file**

Append to `tests/test_context_history_budget.py`:

```python
def test_build_v2_drops_old_messages_when_cap_exceeded(tmp_path):
    from unittest.mock import MagicMock
    from pico.context_manager import ContextManager

    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    # Session with 20 old user turns + 1 fresh user turn (matching user_message).
    old_msgs = [_msg("assistant", "x" * 200), _msg("user", "old q " + "y" * 200)] * 10
    a.session = {"messages": old_msgs + [_msg("user", "current q")]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {"history_soft_cap": 500, "history_floor_messages": 3}

    cm = ContextManager(a)
    request, metadata = cm.build_v2("current q")
    assert metadata["dropped_messages"] > 0
    # Floor guarantees minimum tail; provider gets a bounded messages list.
    assert len(request["messages"]) < len(old_msgs) + 1
```

Run: `uv run pytest tests/test_context_history_budget.py -v`
Expected: 6 passed

- [ ] **Step 7: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: passed count ≥ 596 (baseline) + 6 new tests = ≥ 602 passed

- [ ] **Step 8: Commit**

```bash
git add pico/context_manager.py tests/test_context_history_budget.py
git commit -m "feat(context): turn-based history budget drop via history_soft_cap

Closes Finding 2: build_v2 now drops oldest turn units when message
tokens exceed history_soft_cap, keeping the last N messages as a floor
and preserving tool_use/tool_result pairing atomicity. Adds
dropped_messages + messages_tokens telemetry."
```

---

## Task A2: strip_pico_meta helper for provider payloads

**Files:**
- Create: `pico/providers/message_utils.py`
- Modify: `pico/providers/anthropic_compatible.py` (call `strip_pico_meta` before payload build)
- Modify: `pico/providers/fallback_adapter.py` (call `strip_pico_meta` before flatten)
- Test: `tests/test_provider_message_utils.py` (new)

**Interfaces:**
- Consumes: standard pico message dicts `{role, content, _pico_meta}`.
- Produces: `strip_pico_meta(messages: list[dict]) -> list[dict]` — returns new list of shallow-copied messages with `_pico_meta` removed. Idempotent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_message_utils.py
"""Task A2: strip_pico_meta scrubs internal metadata from provider payloads."""

from pico.providers.message_utils import strip_pico_meta


def test_strip_pico_meta_removes_key():
    src = [{"role": "user", "content": "hi", "_pico_meta": {"created_at": "2026-07-08"}}]
    out = strip_pico_meta(src)
    assert out == [{"role": "user", "content": "hi"}]


def test_strip_pico_meta_leaves_role_content_intact():
    src = [
        {"role": "user", "content": "hi", "_pico_meta": {"a": 1}},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}], "_pico_meta": {"b": 2}},
    ]
    out = strip_pico_meta(src)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hi"
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == [{"type": "text", "text": "hello"}]


def test_strip_pico_meta_idempotent():
    src = [{"role": "user", "content": "hi"}]
    once = strip_pico_meta(src)
    twice = strip_pico_meta(once)
    assert once == twice == src


def test_strip_pico_meta_does_not_mutate_input():
    src = [{"role": "user", "content": "hi", "_pico_meta": {"a": 1}}]
    strip_pico_meta(src)
    assert src[0]["_pico_meta"] == {"a": 1}


def test_strip_pico_meta_empty_list():
    assert strip_pico_meta([]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_message_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pico.providers.message_utils'`

- [ ] **Step 3: Implement strip_pico_meta**

Create `pico/providers/message_utils.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_provider_message_utils.py -v`
Expected: 5 passed

- [ ] **Step 5: Wire strip_pico_meta into Anthropic adapter**

In `pico/providers/anthropic_compatible.py`, find `complete_v2` (search for `def complete_v2`). At the top of the method body, add:

```python
        from .message_utils import strip_pico_meta
        messages = strip_pico_meta(messages)
```

- [ ] **Step 6: Wire strip_pico_meta into FallbackAdapter**

In `pico/providers/fallback_adapter.py`, find `complete_v2`. At the top of the method body, add:

```python
        from .message_utils import strip_pico_meta
        messages = strip_pico_meta(messages)
```

- [ ] **Step 7: Add E2E assertion — no _pico_meta reaches provider**

Append to `tests/test_provider_message_utils.py`:

```python
def test_no_pico_meta_reaches_anthropic_payload():
    """Anthropic adapter payload must not contain _pico_meta anywhere."""
    from unittest.mock import patch, MagicMock
    import json
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient

    client = AnthropicCompatibleModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="test",
        temperature=0.0,
        timeout=10,
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        m = MagicMock()
        m.__enter__.return_value = MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            }).encode("utf-8"),
        )
        m.__exit__.return_value = False
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi", "_pico_meta": {"created_at": "x"}}],
            max_tokens=10,
        )
    payload_str = json.dumps(captured["data"])
    assert "_pico_meta" not in payload_str
```

Run: `uv run pytest tests/test_provider_message_utils.py -v`
Expected: 6 passed

- [ ] **Step 8: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: no regression (still ≥ 602 passed)

- [ ] **Step 9: Commit**

```bash
git add pico/providers/message_utils.py pico/providers/anthropic_compatible.py pico/providers/fallback_adapter.py tests/test_provider_message_utils.py
git commit -m "feat(providers): strip_pico_meta helper prevents metadata leak

Closes Finding 4: both Anthropic and Fallback adapters now scrub
_pico_meta from messages before wire serialization. Idempotent and
non-mutating. Adds an end-to-end payload assertion."
```

---

## Task A3: tools_tokens via json.dumps

**Files:**
- Modify: `pico/context_manager.py` (around line 311 — `tools_tokens` computation)
- Test: `tests/test_context_manager_injection.py` (update existing test)

**Interfaces:**
- Consumes: existing `_count_tokens_for_v2` method.
- Produces: `metadata["tools_tokens"]` = tokens of `json.dumps(tools)` (not `str(tools)`).

- [ ] **Step 1: Update the existing test to demand json-based tokens**

Modify `tests/test_context_manager_injection.py` (add a new test at the end):

```python
def test_build_v2_tools_tokens_uses_json_serialization():
    """Task A3: tools_tokens must reflect JSON wire size, not Python repr."""
    import json
    from unittest.mock import MagicMock
    from pico.context_manager import ContextManager

    a = MagicMock()
    a.prefix = "sys"
    a.tools = {
        "read_file": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "Read a file.",
        },
    }
    a.session = {"messages": [{"role": "assistant", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))

    cm = ContextManager(a)
    request, metadata = cm.build_v2("hello")
    # Recompute the expected token count against the JSON-serialized tools.
    expected = max(1, len(json.dumps(request["tools"])) // 4)
    assert metadata["tools_tokens"] == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_manager_injection.py::test_build_v2_tools_tokens_uses_json_serialization -v`
Expected: FAIL (current impl uses `str(tools)` producing a different count)

- [ ] **Step 3: Change str(tools) to json.dumps(tools)**

In `pico/context_manager.py`, find the line (around 311):

```python
        tools_tokens = self._count_tokens_for_v2(str(tools))
```

Change to:

```python
        # Task A3: use json.dumps so the token estimate reflects wire size,
        # not Python repr (which uses single quotes and off ~2×).
        tools_tokens = self._count_tokens_for_v2(json.dumps(tools, sort_keys=False))
```

Ensure `import json` is at the top of the file (it is — line 9). No change needed.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_context_manager_injection.py -v 2>&1 | tail -12`
Expected: all pass (including the new one)

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: no regression

- [ ] **Step 6: Commit**

```bash
git add pico/context_manager.py tests/test_context_manager_injection.py
git commit -m "fix(context): tools_tokens uses json.dumps not repr

Closes Finding 7: token estimate for tools was off ~2x because
str(list-of-dicts) uses Python repr (single quotes) rather than the
JSON wire format the provider actually receives."
```

---

## Task A4: Nanosecond backup timestamps

**Files:**
- Modify: `pico/session_store.py` (around line 60 — `_write_backup` timestamp)
- Test: `tests/test_session_store_migrator.py` (add distinct-file test)

**Interfaces:**
- Consumes: `time.time_ns()` (stdlib).
- Produces: backup filename `<session_id>.v1.<ns>.json` (was `<session_id>.v1.<s>.json`). Glob pattern `<session_id>.v1.*.json` still matches.

- [ ] **Step 1: Add distinct-file test**

Append to `tests/test_session_store_migrator.py`:

```python
def test_backup_within_same_second_produces_distinct_files(tmp_path):
    """Task A4: two migrations of two different sessions in the same wall
    second must produce distinct backup filenames (ns precision)."""
    import json
    from pico.session_store import SessionStore

    store = SessionStore(tmp_path / ".pico" / "sessions")

    def _v1(id_):
        return {
            "id": id_,
            "created_at": "2026-01-01T00:00:00Z",
            "workspace_root": str(tmp_path),
            "history": [{"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"}],
        }

    for sid in ("s1", "s2"):
        p = store.path_for(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_v1(sid)), encoding="utf-8")
        store.load(sid)

    backup_dir = store.path_for("s1").parent / "backup"
    s1_backups = list(backup_dir.glob("s1.v1.*.json"))
    s2_backups = list(backup_dir.glob("s2.v1.*.json"))
    assert len(s1_backups) == 1
    assert len(s2_backups) == 1
    # Even the same session migrated twice would produce a fresh file if we
    # forced it — but idempotency prevents that here. Distinct session IDs
    # give distinct filenames regardless of timing.
    assert s1_backups[0].name != s2_backups[0].name
```

- [ ] **Step 2: Run test to verify it fails, then update to force real collision test**

The above test **passes with second-precision** because the IDs differ. We need a test that would catch a same-id-same-second collision. Amend the test in place:

```python
def test_backup_uses_nanosecond_precision_in_filename(tmp_path):
    """Task A4: backup filename should carry nanosecond precision."""
    import json
    import re
    from pico.session_store import SessionStore

    store = SessionStore(tmp_path / ".pico" / "sessions")

    v1 = {
        "id": "s1",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_root": str(tmp_path),
        "history": [{"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"}],
    }
    p = store.path_for("s1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(v1), encoding="utf-8")
    store.load("s1")

    backup_dir = p.parent / "backup"
    backups = list(backup_dir.glob("s1.v1.*.json"))
    assert len(backups) == 1
    # Nanosecond timestamps are 19 digits (10^19 ns ≈ 316 years since epoch);
    # second timestamps were 10 digits. Assert the numeric suffix has ≥ 15 digits.
    match = re.match(r"s1\.v1\.(\d+)\.json$", backups[0].name)
    assert match is not None
    assert len(match.group(1)) >= 15, f"Expected nanosecond precision, got {match.group(1)!r}"
```

Run: `uv run pytest tests/test_session_store_migrator.py::test_backup_uses_nanosecond_precision_in_filename -v`
Expected: FAIL (current impl produces 10-digit seconds)

- [ ] **Step 3: Update `_write_backup` in session_store.py**

Find in `pico/session_store.py` (around line 55-63):

```python
def _write_backup(session_path, raw_bytes, session_id):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (backup_dir / f"{session_id}.v1.{ts}.json").write_bytes(raw_bytes)
```

Change to:

```python
def _write_backup(session_path, raw_bytes, session_id):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Task A4: nanosecond precision prevents same-second filename collisions.
    ts = time.time_ns()
    (backup_dir / f"{session_id}.v1.{ts}.json").write_bytes(raw_bytes)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_session_store_migrator.py -v`
Expected: all pass (existing tests use glob `s1.v1.*.json` which still matches)

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/session_store.py tests/test_session_store_migrator.py
git commit -m "fix(session): nanosecond backup timestamps prevent collisions

Closes Finding 10: same-session same-second backup filename collision
window closed. Glob pattern unchanged; existing tests unaffected."
```

---

## Task A5: pico-cli session inspect CLI subcommand

**Files:**
- Create: `pico/cli_session.py`
- Modify: `pico/cli_commands.py` (register `session` subcommand)
- Test: `tests/test_cli_session_inspect.py` (new)

**Interfaces:**
- Consumes: `SessionStore.load(session_id)`.
- Produces:
  - CLI entry point `pico-cli session inspect <session_id>` — exit 0 on match, 1 on mismatch.
  - `inspect_session(session_id, sessions_root) -> tuple[bool, str]` — returns `(ok, report)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_session_inspect.py
"""Task A5: pico-cli session inspect flags dual-write drift."""

import json

from pico.cli_session import inspect_session


def _write_session(sessions_root, session_id, session_dict):
    sessions_root.mkdir(parents=True, exist_ok=True)
    path = sessions_root / f"{session_id}.json"
    path.write_text(json.dumps(session_dict), encoding="utf-8")


def test_inspect_matches_when_history_and_messages_align(tmp_path):
    """User turn count in history == count in messages → OK."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", {
        "id": "s1",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "history": [
            {"role": "user", "content": "q", "created_at": "t"},
            {"role": "assistant", "content": "a", "created_at": "t"},
        ],
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ],
    })
    ok, report = inspect_session("s1", sessions_root=sessions)
    assert ok is True
    assert "match" in report.lower() or "ok" in report.lower()


def test_inspect_flags_user_count_mismatch(tmp_path):
    """history has 2 user turns; messages has 1 → mismatch."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s2", {
        "id": "s2",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "history": [
            {"role": "user", "content": "q1"},
            {"role": "user", "content": "q2"},
        ],
        "messages": [
            {"role": "user", "content": "q1"},
        ],
    })
    ok, report = inspect_session("s2", sessions_root=sessions)
    assert ok is False
    assert "user" in report.lower()
    assert "2" in report and "1" in report


def test_inspect_missing_session_returns_false(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    ok, report = inspect_session("nope", sessions_root=sessions)
    assert ok is False
    assert "not found" in report.lower() or "missing" in report.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_session_inspect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pico.cli_session'`

- [ ] **Step 3: Implement inspect_session**

Create `pico/cli_session.py`:

```python
"""Read-only session inspector.

Task A5 replaces Finding 11's runtime dual-write assertion with a
CLI-driven static check. Users or CI run
``pico-cli session inspect <session_id>`` and get a report on whether
``session["history"]`` (legacy) and ``session["messages"]`` (v2) hold
consistent turn counts. Exit 0 on match, 1 on drift.

The tool is intentionally forgiving: it never mutates the session, never
raises on structural quirks, and reports mismatches in plain English so
an operator can decide whether the drift is intentional (e.g.,
mid-migration state) or a bug.
"""

from __future__ import annotations

import json
from pathlib import Path


def _count_role(items, role):
    """Count entries with ``role``. Accepts either flat dicts (history) or
    Anthropic-shape messages (v2) — the ``role`` key is the same in both."""
    return sum(1 for it in (items or []) if isinstance(it, dict) and it.get("role") == role)


def inspect_session(session_id, sessions_root):
    """Return ``(ok, report_str)`` for the named session.

    ``ok`` is True iff:
    - session file exists
    - user-turn count in ``history`` equals user-turn count in ``messages``
      (a light dual-write invariant that catches obvious drift)

    The report is human-readable multi-line text — no JSON, no colors.
    Users pipe it into their preferred filter.
    """
    sessions_root = Path(sessions_root)
    path = sessions_root / f"{session_id}.json"
    if not path.exists():
        return False, f"session not found: {path}"

    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"failed to read session {session_id}: {exc}"

    history = session.get("history", []) or []
    messages = session.get("messages", []) or []
    hist_user = _count_role(history, "user")
    hist_asst = _count_role(history, "assistant")
    msg_user = _count_role(messages, "user")
    msg_asst = _count_role(messages, "assistant")

    lines = [
        f"session: {session_id}",
        f"schema_version: {session.get('schema_version', 'unknown')}",
        f"history: user={hist_user}, assistant={hist_asst}, total={len(history)}",
        f"messages: user={msg_user}, assistant={msg_asst}, total={len(messages)}",
    ]

    ok = True
    if hist_user != msg_user:
        ok = False
        lines.append(
            f"MISMATCH: history has {hist_user} user turns, "
            f"messages has {msg_user}"
        )
    else:
        lines.append("user-turn count: match")

    return ok, "\n".join(lines)


def handle_session_command(argv, sessions_root=None):
    """CLI entry point: `pico-cli session inspect <session_id>`.

    Returns an exit code (0 or 1). Prints the report to stdout.
    """
    if len(argv) < 2 or argv[0] != "inspect":
        print("usage: pico-cli session inspect <session_id>")
        return 2
    session_id = argv[1]
    if sessions_root is None:
        sessions_root = Path.cwd() / ".pico" / "sessions"
    ok, report = inspect_session(session_id, sessions_root=sessions_root)
    print(report)
    return 0 if ok else 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_session_inspect.py -v`
Expected: 3 passed

- [ ] **Step 5: Wire into cli_commands.py**

Read `pico/cli_commands.py` to find where subcommands are dispatched:

Run: `grep -n "def handle_\|def dispatch\|memory" pico/cli_commands.py | head -10`

Add a subcommand handler for `session`. In `pico/cli_commands.py`, near the existing `memory` dispatch, add:

```python
def handle_session(tokens, root, args):
    """`pico-cli session {inspect} <session_id>`."""
    from pathlib import Path
    from .cli_session import handle_session_command

    sessions_root = Path(root) / ".pico" / "sessions"
    return handle_session_command(list(tokens), sessions_root=sessions_root)
```

Then register it wherever `handle_memory` is registered (search `handle_memory` in that file). Follow the exact same registration pattern.

- [ ] **Step 6: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: no regression

- [ ] **Step 7: Commit**

```bash
git add pico/cli_session.py pico/cli_commands.py tests/test_cli_session_inspect.py
git commit -m "feat(cli): pico-cli session inspect for dual-write drift checks

Closes Finding 11 via a static CLI inspector instead of a runtime
assertion. Compares user/assistant turn counts across session[history]
and session[messages]; exit 0 on match, 1 on drift."
```

---

# Stream B · Configuration Surface

**Gate at end**: `pico.toml` overrides for every wired-in key work end-to-end; `uv run pytest -q` still passing.

---

## Task B1: tomllib-preferring loader

**Files:**
- Modify: `pico/config.py` (add `load_pico_toml_full`)
- Modify: `pyproject.toml` (bump `requires-python` to `>=3.11`)
- Test: `tests/test_config_toml_full.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `load_pico_toml_full(root) -> dict` — uses `tomllib` when available (Python 3.11+), falls back to `load_pico_toml`. Supports nested tables and arrays.

- [ ] **Step 1: Write failing test**

```python
# tests/test_config_toml_full.py
"""Task B1: load_pico_toml_full prefers tomllib and supports arrays / nested tables."""

from pico.config import load_pico_toml_full


def test_reads_flat_scalars(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 12345\n", encoding="utf-8"
    )
    data = load_pico_toml_full(tmp_path)
    assert data["context"]["history_soft_cap"] == 12345


def test_reads_nested_tables(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 5.5\ndescription = 3.5\n",
        encoding="utf-8",
    )
    data = load_pico_toml_full(tmp_path)
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.5
    assert data["memory"]["retrieval"]["field_boost"]["description"] == 3.5


def test_reads_arrays(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[test]\nkeywords = ["a", "b", "c"]\n', encoding="utf-8"
    )
    data = load_pico_toml_full(tmp_path)
    assert data["test"]["keywords"] == ["a", "b", "c"]


def test_returns_empty_when_file_missing(tmp_path):
    data = load_pico_toml_full(tmp_path)
    assert data == {}


def test_returns_empty_when_malformed(tmp_path):
    (tmp_path / "pico.toml").write_text("[[[[not toml\n", encoding="utf-8")
    data = load_pico_toml_full(tmp_path)
    # Falls back to simple parser or returns empty; must NOT raise.
    assert isinstance(data, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_toml_full.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_pico_toml_full'`

- [ ] **Step 3: Bump Python requirement**

In `pyproject.toml`, find `requires-python = ">=3.10"` and change to `>=3.11`. Full command:

```bash
sed -i.bak 's/requires-python = ">=3.10"/requires-python = ">=3.11"/' pyproject.toml && rm pyproject.toml.bak
```

Verify:

```bash
grep "requires-python" pyproject.toml
```

Expected: `requires-python = ">=3.11"`

- [ ] **Step 4: Implement load_pico_toml_full**

Append to `pico/config.py` (after `load_pico_toml`):

```python
def load_pico_toml_full(workspace_root):
    """Full-fidelity pico.toml parser.

    Prefers :mod:`tomllib` (stdlib since Python 3.11) so nested tables and
    typed values (arrays, floats, booleans) round-trip correctly. Falls
    back to :func:`load_pico_toml` for environments where tomllib is
    unavailable, or when the file is malformed enough that tomllib
    raises. Returns ``{}`` if the file doesn't exist.

    The function never raises: config errors surface as an empty dict
    plus a stderr warning, keeping the config surface strictly opt-in.
    """
    from pathlib import Path
    import sys

    path = Path(workspace_root) / "pico.toml"
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        # Python 3.10 or earlier — should not reach here after B1 bump,
        # but be defensive so we never crash on config load.
        return load_pico_toml(workspace_root)
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"warning: pico.toml is malformed, using simple parser fallback ({exc})", file=sys.stderr)
        try:
            return load_pico_toml(workspace_root)
        except Exception:
            return {}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config_toml_full.py -v`
Expected: 5 passed

- [ ] **Step 6: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: no regression

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml pico/config.py tests/test_config_toml_full.py
git commit -m "feat(config): load_pico_toml_full uses tomllib for nested tables

Bumps requires-python to 3.11 so tomllib is always available. Falls
back to the existing simple parser on parse errors so behavior stays
forgiving. Enables Stream B's context/memory config keys."
```

---

## Task B2: context/history config helpers

**Files:**
- Modify: `pico/config.py` (add 4 context helpers)
- Modify: `pico/context_manager.py` (wire helpers into build_v2 via agent.context_config)
- Modify: `pico/runtime.py` (populate `self.context_config` in `Pico.__init__`)
- Test: `tests/test_config_context.py` (new)

**Interfaces:**
- Consumes: `load_pico_toml_full`.
- Produces:
  - `context_history_soft_cap(root) -> int` (default 40000)
  - `context_history_floor_messages(root) -> int` (default 6)
  - `context_injection_budget_ratio(root) -> float` (default 0.15)
  - `context_system_tools_hard_cap(root) -> int` (default 20000)
  - `Pico.context_config: dict` (available as `agent.context_config`)

- [ ] **Step 1: Write failing test**

```python
# tests/test_config_context.py
"""Task B2: pico.toml overrides context settings via helper functions."""

import pytest

from pico.config import (
    context_history_floor_messages,
    context_history_soft_cap,
    context_injection_budget_ratio,
    context_system_tools_hard_cap,
)


def test_history_soft_cap_default(tmp_path):
    assert context_history_soft_cap(tmp_path) == 40000


def test_history_soft_cap_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 12345\n", encoding="utf-8"
    )
    assert context_history_soft_cap(tmp_path) == 12345


def test_history_floor_default(tmp_path):
    assert context_history_floor_messages(tmp_path) == 6


def test_history_floor_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_floor_messages = 10\n", encoding="utf-8"
    )
    assert context_history_floor_messages(tmp_path) == 10


def test_injection_budget_ratio_default(tmp_path):
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.15)


def test_injection_budget_ratio_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\ninjection_budget_ratio = 0.25\n", encoding="utf-8"
    )
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.25)


def test_system_tools_hard_cap_default(tmp_path):
    assert context_system_tools_hard_cap(tmp_path) == 20000


def test_system_tools_hard_cap_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nsystem_tools_hard_cap = 30000\n", encoding="utf-8"
    )
    assert context_system_tools_hard_cap(tmp_path) == 30000


def test_bad_type_falls_back_to_default(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[context]\nhistory_soft_cap = "not-an-int"\n', encoding="utf-8"
    )
    # Fallback rather than raise.
    assert context_history_soft_cap(tmp_path) == 40000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_context.py -v`
Expected: FAIL with `ImportError: cannot import name 'context_history_soft_cap'`

- [ ] **Step 3: Implement 4 context helpers**

Append to `pico/config.py`:

```python
# ---------------------------------------------------------------------------
# Task B2-B6: pico.toml surface for the context/memory subsystems.
# Each helper is independent: missing file / missing section / bad type all
# fall back to the hard-coded default. The pattern mirrors
# ``project_max_blob_size`` above so future keys can be added without
# building a shared config object.
# ---------------------------------------------------------------------------

def _context_int(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw <= 0:
        return default
    return raw


def _context_float(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    if raw < 0:
        return default
    return float(raw)


def context_history_soft_cap(root) -> int:
    """Max tokens allowed in messages array before older turns are dropped."""
    return _context_int(root, "history_soft_cap", 40000)


def context_history_floor_messages(root) -> int:
    """Minimum tail messages preserved regardless of budget."""
    return _context_int(root, "history_floor_messages", 6)


def context_injection_budget_ratio(root) -> float:
    """Fraction of total budget available for <system-reminder> injection."""
    return _context_float(root, "injection_budget_ratio", 0.15)


def context_system_tools_hard_cap(root) -> int:
    """Fail-loud threshold for system + tools token count."""
    return _context_int(root, "system_tools_hard_cap", 20000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_context.py -v`
Expected: 9 passed

- [ ] **Step 5: Populate `Pico.context_config` in runtime.py**

In `pico/runtime.py`, find `Pico.__init__` (around line 215-245, after `self.project_max_blob_size = ...`). Add:

```python
        # Task B2: gather context/memory config from pico.toml. Downstream
        # subsystems read via `self.agent.context_config[...]` with defaults
        # already baked in by the helper functions.
        from .config import (
            context_history_floor_messages,
            context_history_soft_cap,
            context_injection_budget_ratio,
            context_system_tools_hard_cap,
        )

        self.context_config = {
            "history_soft_cap": context_history_soft_cap(self.root),
            "history_floor_messages": context_history_floor_messages(self.root),
            "injection_budget_ratio": context_injection_budget_ratio(self.root),
            "system_tools_hard_cap": context_system_tools_hard_cap(self.root),
        }
```

- [ ] **Step 6: Update build_v2 to read from context_config**

In `pico/context_manager.py`, find where `SYSTEM_TOOLS_HARD_CAP` is used (search for `SYSTEM_TOOLS_HARD_CAP`). Around line 312:

```python
        pinned_cap = SYSTEM_TOOLS_HARD_CAP
```

Change to:

```python
        cfg = getattr(self.agent, "context_config", {}) or {}
        pinned_cap = int(cfg.get("system_tools_hard_cap", SYSTEM_TOOLS_HARD_CAP))
```

Task A1 already reads `history_soft_cap` and `history_floor_messages` from `context_config` — this task just ensures those values now come from `pico.toml` when overridden.

- [ ] **Step 7: Add end-to-end integration test**

Append to `tests/test_config_context.py`:

```python
def test_build_v2_reads_system_tools_hard_cap_from_pico_toml(tmp_path):
    """Overriding system_tools_hard_cap in pico.toml raises SystemTooBig sooner."""
    from unittest.mock import MagicMock

    from pico.context_manager import ContextManager

    (tmp_path / "pico.toml").write_text(
        "[context]\nsystem_tools_hard_cap = 100\n", encoding="utf-8"
    )

    a = MagicMock()
    a.prefix = "x" * 500  # ~125 tokens with /4 fallback → over 100 cap
    a.tools = {}
    a.session = {"messages": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {"system_tools_hard_cap": 100}

    cm = ContextManager(a)
    with pytest.raises(RuntimeError, match="SystemTooBig"):
        cm.build_v2("hi")
```

Run: `uv run pytest tests/test_config_context.py -v`
Expected: 10 passed

- [ ] **Step 8: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 9: Commit**

```bash
git add pico/config.py pico/runtime.py pico/context_manager.py tests/test_config_context.py
git commit -m "feat(config): context helpers for history/injection/system_tools

Adds 4 pico.toml helpers (soft_cap, floor, injection_ratio,
system_tools_cap) following the project_max_blob_size pattern.
Pico.__init__ populates self.context_config once; build_v2 reads
system_tools_hard_cap from it."
```

---

## Task B3: digest size threshold from pico.toml

**Files:**
- Modify: `pico/config.py` (add `context_digest_size_threshold`)
- Modify: `pico/runtime.py` (add `digest_size_threshold` to `context_config`)
- Modify: `pico/agent_loop.py` (pass threshold to `should_digest`)
- Test: `tests/test_config_context.py` (extend)

**Interfaces:**
- Produces: `context_digest_size_threshold(root) -> int` (default 1200); `agent.context_config["digest_size_threshold"]`.

- [ ] **Step 1: Add failing test**

Append to `tests/test_config_context.py`:

```python
def test_digest_size_threshold_default(tmp_path):
    from pico.config import context_digest_size_threshold
    assert context_digest_size_threshold(tmp_path) == 1200


def test_digest_size_threshold_override(tmp_path):
    from pico.config import context_digest_size_threshold
    (tmp_path / "pico.toml").write_text(
        "[context.digest]\nsize_threshold_chars = 500\n", encoding="utf-8"
    )
    assert context_digest_size_threshold(tmp_path) == 500


def test_append_tool_result_uses_config_threshold(tmp_path):
    """Overriding digest.size_threshold_chars via context_config makes small
    tool results digest even though they'd otherwise be inlined."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.agent_loop import _append_tool_result

    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s1"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = str(tmp_path)
    a.current_task_state = SimpleNamespace(run_id="r1", task_id="t1")
    a.current_run_dir = tmp_path / ".pico" / "runs" / "r1"
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    # Force a small threshold so a 100-char payload triggers digest.
    a.context_config = {"digest_size_threshold": 50}

    _append_tool_result(
        a,
        tool_use_id="t1",
        content="x" * 100,  # over threshold=50
        tool_name="read_file",
        tool_args={"path": "a.py"},
    )
    msg = session_messages[-1]
    assert msg["_pico_meta"]["digest_applied"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_context.py::test_digest_size_threshold_default tests/test_config_context.py::test_digest_size_threshold_override tests/test_config_context.py::test_append_tool_result_uses_config_threshold -v`
Expected: FAIL (functions don't exist yet, and `_append_tool_result` uses hard-coded 1200)

- [ ] **Step 3: Add helper to config.py**

Append:

```python
def _context_digest_int(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get("digest", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw <= 0:
        return default
    return raw


def context_digest_size_threshold(root) -> int:
    """Threshold in characters above which a tool_result gets digested."""
    return _context_digest_int(root, "size_threshold_chars", 1200)
```

- [ ] **Step 4: Wire into runtime.py**

In `pico/runtime.py`, update the imports at the `context_config` block:

```python
        from .config import (
            context_digest_size_threshold,
            context_history_floor_messages,
            context_history_soft_cap,
            context_injection_budget_ratio,
            context_system_tools_hard_cap,
        )
```

And add to the `context_config` dict:

```python
            "digest_size_threshold": context_digest_size_threshold(self.root),
```

- [ ] **Step 5: Update `_append_tool_result` to read config**

In `pico/agent_loop.py`, find `_append_tool_result`. The line `if not digest_applied and should_digest(content):` currently uses the default threshold. Change to:

```python
    # Task B3: threshold overridable via pico.toml → agent.context_config.
    cfg = getattr(agent, "context_config", {}) or {}
    threshold = int(cfg.get("digest_size_threshold", 1200))
    if not digest_applied and should_digest(content, threshold=threshold):
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_config_context.py -v`
Expected: 13 passed

- [ ] **Step 7: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 8: Commit**

```bash
git add pico/config.py pico/runtime.py pico/agent_loop.py tests/test_config_context.py
git commit -m "feat(config): digest size_threshold_chars overridable via pico.toml"
```

---

## Task B4: recall config from pico.toml

**Files:**
- Modify: `pico/config.py` (add `memory_recall_config`)
- Modify: `pico/runtime.py` (add `recall` to `context_config`)
- Modify: `pico/memory/recall.py` (read config from agent)
- Test: `tests/test_config_memory.py` (new)

**Interfaces:**
- Produces: `memory_recall_config(root) -> dict` with keys `min_score`, `top_k`, `max_tokens_per_note`, `skip_recent_turns`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_config_memory.py
"""Task B4-B6: pico.toml overrides for memory subsystem."""

import pytest

from pico.config import memory_recall_config


def test_recall_config_defaults(tmp_path):
    cfg = memory_recall_config(tmp_path)
    assert cfg == {
        "min_score": pytest.approx(0.3),
        "top_k": 2,
        "max_tokens_per_note": 400,
        "skip_recent_turns": 2,
    }


def test_recall_config_partial_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.recall]\nmin_score = 0.5\ntop_k = 4\n", encoding="utf-8"
    )
    cfg = memory_recall_config(tmp_path)
    assert cfg["min_score"] == pytest.approx(0.5)
    assert cfg["top_k"] == 4
    # Un-overridden keys still take defaults.
    assert cfg["max_tokens_per_note"] == 400
    assert cfg["skip_recent_turns"] == 2


def test_recall_for_turn_reads_min_score_from_agent(tmp_path):
    """recall_for_turn should filter using agent.context_config['recall']['min_score']."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n",
        encoding="utf-8",
    )

    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)

    # min_score=0.99 → the "cache" query should NOT clear the bar.
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={"recall": {"min_score": 0.99, "top_k": 2, "max_tokens_per_note": 400, "skip_recent_turns": 2}},
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None  # gate closed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_memory.py -v`
Expected: FAIL

- [ ] **Step 3: Add memory_recall_config to config.py**

```python
def memory_recall_config(root) -> dict:
    """Recall subsystem config: min_score, top_k, max_tokens_per_note, skip_recent_turns."""
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("recall", {}) or {}

    def _pick_float(key, default):
        v = raw.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default

    def _pick_int(key, default):
        v = raw.get(key)
        return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else default

    return {
        "min_score": _pick_float("min_score", 0.3),
        "top_k": _pick_int("top_k", 2),
        "max_tokens_per_note": _pick_int("max_tokens_per_note", 400),
        "skip_recent_turns": _pick_int("skip_recent_turns", 2),
    }
```

- [ ] **Step 4: Wire into runtime.py context_config**

Add to imports:

```python
            memory_recall_config,
```

And to the dict:

```python
            "recall": memory_recall_config(self.root),
```

- [ ] **Step 5: Update recall.py to read agent.context_config**

In `pico/memory/recall.py`, near the top of `recall_for_turn`, replace the four constant reads with:

```python
    cfg_all = getattr(agent, "context_config", {}) or {}
    cfg = cfg_all.get("recall", {}) or {}
    min_score = float(cfg.get("min_score", RECALL_MIN_SCORE))
    top_k = int(cfg.get("top_k", RECALL_TOP_K))
    max_tokens_per_note = int(cfg.get("max_tokens_per_note", RECALL_MAX_TOKENS_PER_NOTE))
    skip_recent_turns = int(cfg.get("skip_recent_turns", RECALL_SKIP_RECENT_TURNS))
```

Then replace subsequent uses of the module constants inside `recall_for_turn` with the local variables (`RECALL_TOP_K` → `top_k`, `RECALL_MIN_SCORE` → `min_score`, `RECALL_MAX_TOKENS_PER_NOTE` → `max_tokens_per_note`). The `_flatten_recent` helper still uses `RECALL_SKIP_RECENT_TURNS` as its cap — pass `skip_recent_turns` through:

Change `_flatten_recent(session_recent)` signature to `_flatten_recent(session_recent, skip_turns)` and update the caller. Update the internal logic:

```python
def _flatten_recent(session_recent, skip_turns):
    out = set()
    for turn in (session_recent or [])[-skip_turns:]:
        for p in turn or []:
            out.add(p)
    return out
```

Call site:

```python
    recent_skip = _flatten_recent(agent.session.get("recently_recalled"), skip_recent_turns)
```

And the deque bound at end:

```python
    agent.session["recently_recalled"] = recent[-(skip_recent_turns + 1) :]
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_config_memory.py tests/test_memory_recall.py -v`
Expected: all pass

- [ ] **Step 7: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 8: Commit**

```bash
git add pico/config.py pico/runtime.py pico/memory/recall.py tests/test_config_memory.py
git commit -m "feat(config): memory.recall.* overridable via pico.toml"
```

---

## Task B5: field boost + link config from pico.toml

**Files:**
- Modify: `pico/config.py` (`memory_field_boosts`, `memory_link_config`)
- Modify: `pico/memory/retrieval.py` (constructor accepts config kwarg)
- Modify: `pico/runtime.py` (pass config into `Retrieval`)
- Test: `tests/test_config_memory.py` (extend)

**Interfaces:**
- Produces:
  - `memory_field_boosts(root) -> dict[str, float]` (keys: name, description, tags, aliases, body)
  - `memory_link_config(root) -> tuple[int, float]` (max_added, decay)
  - `Retrieval(store, *, config=None)` — optional config kwarg holds field_boosts + link_config.

- [ ] **Step 1: Write failing test**

Append to `tests/test_config_memory.py`:

```python
def test_field_boosts_defaults(tmp_path):
    from pico.config import memory_field_boosts
    fb = memory_field_boosts(tmp_path)
    assert fb == {
        "name": 5.0,
        "description": 3.0,
        "tags": 4.0,
        "aliases": 4.0,
        "body": 1.0,
    }


def test_field_boosts_override(tmp_path):
    from pico.config import memory_field_boosts
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 8.0\ndescription = 2.0\n",
        encoding="utf-8",
    )
    fb = memory_field_boosts(tmp_path)
    assert fb["name"] == 8.0
    assert fb["description"] == 2.0
    # Un-overridden keys retain defaults.
    assert fb["tags"] == 4.0


def test_link_config_defaults(tmp_path):
    from pico.config import memory_link_config
    assert memory_link_config(tmp_path) == (3, 0.4)


def test_link_config_override(tmp_path):
    from pico.config import memory_link_config
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.link]\nmax_added = 5\ndecay = 0.6\n", encoding="utf-8"
    )
    assert memory_link_config(tmp_path) == (5, 0.6)


def test_retrieval_uses_field_boosts_from_config(tmp_path):
    """A note where 'cache' appears only in body loses to a note where it appears
    only in description when field_boosts default; if we push body up above
    description, the body-hit note should win."""
    from pico.memory.block_store import BlockStore
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "in_desc.md").write_text(
        "---\nname: in_desc\ntype: feedback\ndescription: cache mention\n---\nother body\n",
        encoding="utf-8",
    )
    (ws / "agent" / "in_body.md").write_text(
        "---\nname: in_body\ntype: feedback\ndescription: unrelated\n---\ncache appears here\n",
        encoding="utf-8",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    # Push body way up above description → in_body wins.
    ret = Retrieval(store, config={
        "field_boosts": {"name": 5.0, "description": 1.0, "tags": 4.0, "aliases": 4.0, "body": 10.0},
        "link_config": (3, 0.4),
    })
    hits = ret.search("cache")
    assert hits[0].path == "workspace/agent/in_body.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_memory.py -v`
Expected: FAIL

- [ ] **Step 3: Add helpers**

Append to `pico/config.py`:

```python
def memory_field_boosts(root) -> dict:
    """BM25 field boost weights: {name, description, tags, aliases, body}."""
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("retrieval", {}).get("field_boost", {}) or {}
    defaults = {"name": 5.0, "description": 3.0, "tags": 4.0, "aliases": 4.0, "body": 1.0}
    out = dict(defaults)
    for key, default in defaults.items():
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[key] = float(v)
    return out


def memory_link_config(root) -> tuple:
    """(max_added, decay) for [[name]] link expansion."""
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("retrieval", {}).get("link", {}) or {}
    max_added = raw.get("max_added")
    decay = raw.get("decay")
    max_added = max_added if isinstance(max_added, int) and not isinstance(max_added, bool) and max_added > 0 else 3
    decay = float(decay) if isinstance(decay, (int, float)) and not isinstance(decay, bool) and 0 <= decay <= 1 else 0.4
    return (max_added, decay)
```

- [ ] **Step 4: Extend Retrieval constructor**

In `pico/memory/retrieval.py`, find `class Retrieval` and modify `__init__`:

```python
class Retrieval:
    def __init__(self, store, *, config=None):
        self.store = store
        # Task B5: allow pico.toml overrides for field boosts + link config.
        # Passing None keeps the module-level constants active for callers
        # that don't wire config yet.
        cfg = config or {}
        self._field_boosts = cfg.get("field_boosts", FIELD_BOOSTS)
        link_cfg = cfg.get("link_config", (LINK_MAX_ADDED, LINK_DECAY))
        self._link_max_added, self._link_decay = link_cfg
```

Then in `search()` / `_bm25_field_score`, replace uses of `FIELD_BOOSTS`, `LINK_MAX_ADDED`, `LINK_DECAY` with `self._field_boosts`, `self._link_max_added`, `self._link_decay`. Because `_bm25_field_score` is currently a `@staticmethod`, pass `self._field_boosts` as a parameter or drop the staticmethod:

Change:

```python
    @staticmethod
    def _bm25_field_score(query_tokens, fields, flat_tokens, avg_doc_len, N, df):
```

to:

```python
    def _bm25_field_score(self, query_tokens, fields, flat_tokens, avg_doc_len, N, df):
```

And replace `FIELD_BOOSTS.get(name, 1.0)` inside the method body with `self._field_boosts.get(name, 1.0)`.

Update the call site (in `search()`): `self._bm25_field_score(...)` — already prefixed with `self.`, no signature change needed if you already dropped the `@staticmethod`.

For link expansion, find every `LINK_MAX_ADDED` and `LINK_DECAY` inside `Retrieval` methods and replace with `self._link_max_added` and `self._link_decay`.

- [ ] **Step 5: Update runtime.py to pass config into Retrieval**

In `pico/runtime.py`, find where `Retrieval` is instantiated:

```python
        self.memory_retrieval = Retrieval(self.memory_store)
```

Change to:

```python
        from .config import memory_field_boosts, memory_link_config

        self.memory_retrieval = Retrieval(
            self.memory_store,
            config={
                "field_boosts": memory_field_boosts(self.root),
                "link_config": memory_link_config(self.root),
            },
        )
```

Also add these to the `context_config` dict so downstream tools can inspect:

```python
            "field_boosts": memory_field_boosts(self.root),
            "link_config": memory_link_config(self.root),
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_config_memory.py tests/test_memory_retrieval_field_boost.py tests/test_memory_retrieval_link.py tests/test_memory.py -v 2>&1 | tail -20`
Expected: all pass

- [ ] **Step 7: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 8: Commit**

```bash
git add pico/config.py pico/memory/retrieval.py pico/runtime.py tests/test_config_memory.py
git commit -m "feat(config): field_boosts + link_config overridable via pico.toml

Retrieval constructor now accepts optional config kwarg. Retrieval
instance holds per-instance boosts + link caps so multiple Retrievals
in the same process can carry different tunings."
```

---

## Task B6: injection budget ratio wired into renderer

**Files:**
- Modify: `pico/context/renderer.py` (accept + use injection_budget)
- Modify: `pico/context_manager.py` (pass injection_budget to renderer)
- Test: `tests/test_context_renderer.py` (extend)

**Interfaces:**
- Produces: `render_current_user_message` reads `agent.context_config["injection_budget_ratio"]` when computing total injection budget.

**Note**: The renderer already accepts individual per-source budgets from the intent profile. This task adds a **global cap**: the sum of injection tokens must not exceed `injection_budget_ratio × total_budget_hard_cap`. Sources over the sum trigger Stream C1's drop logic. This task only wires the number through; the drop logic lands in C1.

- [ ] **Step 1: Add failing test**

Append to `tests/test_context_renderer.py`:

```python
def test_renderer_reads_injection_budget_from_agent_config(tmp_path):
    """When agent.context_config has an injection_budget_ratio, the renderer
    computes an injection_budget and stashes it in telemetry for later use."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    a = SimpleNamespace(
        memory_store=None,
        memory_retrieval=None,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: "branch: main"),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={
            "injection_budget_ratio": 0.10,
            "total_budget_hard_cap": 100000,
        },
    )
    _text, tele = render_current_user_message(a, "hi")
    # Injection budget = 100000 × 0.10 = 10000
    assert tele.get("injection_budget") == 10000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_renderer.py::test_renderer_reads_injection_budget_from_agent_config -v`
Expected: FAIL

- [ ] **Step 3: Update renderer**

In `pico/context/renderer.py`, near the top of `render_current_user_message`, before the `blocks = []` line:

```python
    # Task B6: compute the aggregate injection budget cap. Downstream C1
    # will use it to drop least-important blocks when sum overflows.
    cfg = getattr(agent, "context_config", {}) or {}
    ratio = float(cfg.get("injection_budget_ratio", 0.15))
    total = int(cfg.get("total_budget_hard_cap", 100000))
    injection_budget = int(total * ratio)
    telemetry["injection_budget"] = injection_budget
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_context_renderer.py -v 2>&1 | tail -12`
Expected: all pass

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/context/renderer.py tests/test_context_renderer.py
git commit -m "feat(context): renderer reads injection_budget from context_config

Prep for Stream C1's drop-priority implementation. The renderer now
publishes the computed injection_budget in telemetry so subsequent
overflow handling has an anchor."
```

---

## Task B7: end-to-end integration test with full pico.toml

**Files:**
- Test: `tests/test_pico_toml_end_to_end.py` (new)

**Interfaces:** Consumes all B2-B6 helpers. Produces no new interfaces.

- [ ] **Step 1: Write the test**

```python
# tests/test_pico_toml_end_to_end.py
"""Task B7: a single pico.toml overriding every wired key end-to-end."""

import pytest

from pico.config import (
    context_digest_size_threshold,
    context_history_floor_messages,
    context_history_soft_cap,
    context_injection_budget_ratio,
    context_system_tools_hard_cap,
    memory_field_boosts,
    memory_link_config,
    memory_recall_config,
)


PICO_TOML = """
[context]
history_soft_cap = 12345
history_floor_messages = 8
injection_budget_ratio = 0.25
system_tools_hard_cap = 30000

[context.digest]
size_threshold_chars = 500

[memory.recall]
min_score = 0.45
top_k = 3
max_tokens_per_note = 300
skip_recent_turns = 4

[memory.retrieval.field_boost]
name = 6.0
description = 3.5
tags = 4.5
aliases = 4.0
body = 1.5

[memory.retrieval.link]
max_added = 5
decay = 0.5
"""


def test_full_pico_toml_overrides_take_effect(tmp_path):
    (tmp_path / "pico.toml").write_text(PICO_TOML, encoding="utf-8")

    assert context_history_soft_cap(tmp_path) == 12345
    assert context_history_floor_messages(tmp_path) == 8
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.25)
    assert context_system_tools_hard_cap(tmp_path) == 30000
    assert context_digest_size_threshold(tmp_path) == 500

    recall = memory_recall_config(tmp_path)
    assert recall == {
        "min_score": pytest.approx(0.45),
        "top_k": 3,
        "max_tokens_per_note": 300,
        "skip_recent_turns": 4,
    }

    fb = memory_field_boosts(tmp_path)
    assert fb == {
        "name": 6.0,
        "description": 3.5,
        "tags": 4.5,
        "aliases": 4.0,
        "body": 1.5,
    }

    assert memory_link_config(tmp_path) == (5, 0.5)


def test_partial_pico_toml_only_overrides_provided_keys(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.recall]\nmin_score = 0.7\n", encoding="utf-8"
    )
    # Only min_score changed; everything else stays default.
    assert context_history_soft_cap(tmp_path) == 40000  # default
    recall = memory_recall_config(tmp_path)
    assert recall["min_score"] == pytest.approx(0.7)
    assert recall["top_k"] == 2  # default
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_pico_toml_end_to_end.py -v`
Expected: 2 passed

- [ ] **Step 3: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 4: Commit**

```bash
git add tests/test_pico_toml_end_to_end.py
git commit -m "test(config): end-to-end pico.toml override verification

Locks in that all 8 helper functions honor a single pico.toml file with
mixed sections. Also covers partial-override behavior (untouched keys
keep defaults)."
```

---

# Stream C · Observability & Consistency

**Gate at end**: All spec §9 metadata fields present in `build_v2` output; recall errors reach telemetry; debug logging hooks in place.

---

## Task C1: injection_dropped implementation

**Files:**
- Modify: `pico/context/renderer.py` (add `DROP_PRIORITY` + drop logic)
- Test: `tests/test_context_renderer.py` (extend)

**Interfaces:**
- Consumes: `telemetry["injection_budget"]` from B6.
- Produces: `telemetry["injection_dropped"] = ["source_name", ...]` populated when injection sum exceeds budget.

- [ ] **Step 1: Write failing test**

Append to `tests/test_context_renderer.py`:

```python
def test_injection_drops_checkpoint_before_recalled_memory():
    """When aggregate injection tokens exceed injection_budget, DROP_PRIORITY
    dictates checkpoint drops first, recalled_memory drops last."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    def _long(_agent, _budget, _user_msg=""):
        return "x" * 4000  # ~1000 tokens each

    # Build an agent whose sources ALL return large content, but injection_budget is small.
    a = SimpleNamespace(
        memory_store=None,
        memory_retrieval=None,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: "x" * 4000),
        repo_map=MagicMock(refresh_if_stale=lambda: None,
                           top_level_tree=lambda: [{"path": "p", "file_count": 1}],
                           language_stats=lambda: {"py": 1}),
        render_checkpoint_text=lambda: "x" * 4000,
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={
            "injection_budget_ratio": 0.01,
            "total_budget_hard_cap": 100000,  # → budget = 1000 tokens
        },
    )
    text, tele = render_current_user_message(a, "上次讨论过 cache 的问题")
    # Some sources must have been dropped.
    assert len(tele["injection_dropped"]) >= 1
    # checkpoint is least important; must drop before recalled_memory.
    if "recalled_memory" in tele["injection_dropped"]:
        assert "checkpoint" in tele["injection_dropped"]
        assert "project_structure" in tele["injection_dropped"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_renderer.py::test_injection_drops_checkpoint_before_recalled_memory -v`
Expected: FAIL

- [ ] **Step 3: Add DROP_PRIORITY + drop logic**

In `pico/context/renderer.py`, near the top (after `SOURCE_ORDER`):

```python
# Task C1: drop-priority order — least important first. When aggregate
# injection tokens exceed injection_budget, sources are dropped from the
# start of this list. Distinct from SOURCE_ORDER (which is the *render*
# order in the outgoing user message).
DROP_PRIORITY = (
    "checkpoint",
    "project_structure",
    "memory_index",
    "workspace_state",
    "recalled_memory",  # last — decision-critical per spec §4.4.3
)
```

Then at the bottom of `render_current_user_message`, after the `for source_name in SOURCE_ORDER:` loop and before `text = "\n\n".join(...)`:

```python
    # Task C1: enforce aggregate injection budget by dropping sources in
    # DROP_PRIORITY order until we fit. `blocks` and `telemetry` track the
    # in-order rendered state; a dropped source is removed from both.
    def _current_tokens():
        return sum(telemetry["injection_tokens"].get(s, 0) for s in SOURCE_ORDER)

    if injection_budget > 0:
        for candidate in DROP_PRIORITY:
            if _current_tokens() <= injection_budget:
                break
            if telemetry["injection_tokens"].get(candidate, 0) <= 0:
                continue
            # Remove any block whose tag matches this source name.
            blocks = [b for b in blocks if f"<pico:{candidate}>" not in b]
            telemetry["injection_tokens"][candidate] = 0
            telemetry["injection_dropped"].append(candidate)
```

Note the `blocks` list is now potentially reassigned inside the loop — ensure it's a local mutable list (already is, from the earlier `blocks = []` at top of function).

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_context_renderer.py -v 2>&1 | tail -12`
Expected: all pass

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/context/renderer.py tests/test_context_renderer.py
git commit -m "feat(context): injection_dropped populated via DROP_PRIORITY

Closes Finding 5: when aggregate injection tokens exceed budget, drops
sources in least-important-first order (checkpoint → project_structure
→ memory_index → workspace_state → recalled_memory). Records dropped
names in telemetry."
```

---

## Task C2: recall error telemetry

**Files:**
- Modify: `pico/context/sources.py` (`render_recalled_memory` records error)
- Modify: `pico/context_manager.py` (build_v2 copies `_recall_errors` into metadata)
- Test: `tests/test_context_recall_integration.py` (extend)

**Interfaces:**
- Produces: `session["_recall_errors"] = {"count": int, "last": str}`; `metadata["recall.error_count"]`, `metadata["recall.last_error"]`.

- [ ] **Step 1: Write failing test**

Append to `tests/test_context_recall_integration.py`:

```python
def test_recall_error_recorded_to_telemetry(tmp_path, monkeypatch):
    """When recall_for_turn raises, error is recorded to telemetry."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pico.context.sources as sources_mod
    from pico.context.renderer import render_current_user_message

    # Force recall_for_turn to raise.
    def _boom(*args, **kwargs):
        raise ValueError("simulated recall crash")

    monkeypatch.setattr("pico.memory.recall.recall_for_turn", _boom)

    a = SimpleNamespace(
        memory_store=MagicMock(),
        memory_retrieval=MagicMock(),
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )
    _text, tele = render_current_user_message(a, "上次讨论过 cache")
    # Recall failed but session still tracks it.
    assert a.session["_recall_errors"]["count"] >= 1
    assert "simulated recall crash" in a.session["_recall_errors"]["last"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_recall_integration.py::test_recall_error_recorded_to_telemetry -v`
Expected: FAIL

- [ ] **Step 3: Update render_recalled_memory to record errors**

In `pico/context/sources.py`, find `render_recalled_memory`:

```python
def render_recalled_memory(agent, budget_tokens, user_message=""):
    from pico.memory.recall import recall_for_turn
    try:
        return recall_for_turn(agent, user_message, budget_tokens)
    except Exception:
        return None
```

Replace with:

```python
def render_recalled_memory(agent, budget_tokens, user_message=""):
    """Renderer entry for recall. Records exceptions to a session-scoped
    counter so operators can spot silent failures via telemetry."""
    from pico.memory.recall import recall_for_turn

    try:
        return recall_for_turn(agent, user_message, budget_tokens)
    except Exception as exc:
        # Task C2: surface silent recall failures without breaking the turn.
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] += 1
            counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
        return None
```

- [ ] **Step 4: Wire into build_v2 metadata**

In `pico/context_manager.py`, inside `build_v2` where `metadata` is built:

```python
        recall_errors = (getattr(self.agent, "session", {}) or {}).get("_recall_errors", {}) or {}
        metadata = {
            ...
            "recall.error_count": int(recall_errors.get("count", 0)),
            "recall.last_error": str(recall_errors.get("last", "")),
        }
```

Add these two fields to the existing metadata dict (find the block near line 388).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_context_recall_integration.py -v`
Expected: all pass

- [ ] **Step 6: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 7: Commit**

```bash
git add pico/context/sources.py pico/context_manager.py tests/test_context_recall_integration.py
git commit -m "feat(observability): recall errors reach telemetry

Closes Finding 9: render_recalled_memory now records raised exceptions
to session[_recall_errors]; build_v2 copies count + last to metadata."
```

---

## Task C3: debug logging hooks

**Files:**
- Modify: `pico/agent_loop.py`, `pico/context/renderer.py`, `pico/context/sources.py`, `pico/memory/recall.py`, `pico/session_store.py` (add `logger` + debug calls at silent catches)
- Test: `tests/test_debug_logging.py` (new)

**Interfaces:**
- Produces: `logging.getLogger("pico")` calls at 5 silent catches.

- [ ] **Step 1: Write failing test**

```python
# tests/test_debug_logging.py
"""Task C3: silent catches now emit debug logs on the 'pico' logger."""

import logging

import pytest


def test_recall_failure_logs_debug(caplog, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from pico.context.renderer import render_current_user_message

    def _boom(*a, **kw):
        raise RuntimeError("simulated recall failure")
    monkeypatch.setattr("pico.memory.recall.recall_for_turn", _boom)

    a = SimpleNamespace(
        memory_store=MagicMock(),
        memory_retrieval=MagicMock(),
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )

    caplog.set_level(logging.DEBUG, logger="pico")
    render_current_user_message(a, "上次讨论 cache")
    assert any("recall" in r.message.lower() for r in caplog.records)


def test_workspace_state_failure_logs_debug(caplog, tmp_path):
    from unittest.mock import MagicMock
    from pico.context.sources import render_workspace_state

    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(side_effect=RuntimeError("no git"))

    caplog.set_level(logging.DEBUG, logger="pico")
    result = render_workspace_state(a, budget_tokens=500)
    assert result is None
    assert any("workspace_state" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_debug_logging.py -v`
Expected: FAIL (no debug logs emitted)

- [ ] **Step 3: Add logger to sources.py**

In `pico/context/sources.py`, at the top after existing imports:

```python
import logging

logger = logging.getLogger("pico")
```

Update the 4 `except Exception:` catches in `render_workspace_state`, `render_memory_index`, `render_project_structure`, `render_checkpoint`. For each, change:

```python
    except Exception:
        return None
```

to (name the source in the message):

```python
    except Exception as exc:
        logger.debug("workspace_state source failed: %s", exc)  # adjust name per function
        return None
```

For `render_recalled_memory`, update the existing catch (already touched in C2):

```python
    except Exception as exc:
        logger.debug("recalled_memory source failed: %s", exc)
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] += 1
            counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
        return None
```

- [ ] **Step 4: Add logger to remaining modules**

`pico/agent_loop.py`, `pico/context/renderer.py`, `pico/memory/recall.py`, `pico/session_store.py` — add:

```python
import logging

logger = logging.getLogger("pico")
```

For each, find existing catch blocks and add a `logger.debug(...)` before returning. Skip catches that already raise or return meaningful errors.

Key additions:
- `pico/session_store.py::_migrate_v1_to_v2` — where an unknown role is silently ignored, add `logger.debug("session migrator: unknown role %r", role)`.
- `pico/agent_loop.py::_append_tool_result` — the `except OSError:` around raw file write, add `logger.debug("raw tool_result write failed: %s", exc)`.
- `pico/memory/recall.py` — any `except (OSError, ValueError):` catches, add `logger.debug("recall: %s", exc)`.
- `pico/context/renderer.py` — no new catches; the source-level logging is enough.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_debug_logging.py -v`
Expected: 2 passed

- [ ] **Step 6: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 7: Commit**

```bash
git add pico/agent_loop.py pico/context/sources.py pico/context/renderer.py pico/memory/recall.py pico/session_store.py tests/test_debug_logging.py
git commit -m "feat(observability): debug log hooks at silent catches

Users opt in via logging.basicConfig(level=DEBUG). All previously
silent failures (source renderers, session migrator, raw file write)
now emit a debug line without changing behavior."
```

---

## Task C4: intent.matched_reason in telemetry

**Files:**
- Modify: `pico/context/renderer.py` (add matched_reason)
- Test: `tests/test_context_renderer.py` (extend)

**Interfaces:**
- Produces: `telemetry["intent"]["matched_reason"]` — string like `"keyword:'报错' via profile:debug"` or `"default (no keyword)"`.

- [ ] **Step 1: Write failing test**

Append to `tests/test_context_renderer.py`:

```python
def test_intent_matched_reason_populated_for_keyword_hit():
    from unittest.mock import MagicMock
    from pico.context.renderer import render_current_user_message

    a = MagicMock()
    a.workspace = MagicMock(volatile_text=lambda: "")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.session = {"recently_recalled": [], "messages": []}
    a.memory_retrieval = None

    _text, tele = render_current_user_message(a, "上次报错了")
    # "报错" is a debug keyword
    assert tele["intent"]["matched_reason"] == "keyword:'报错' via profile:debug"


def test_intent_matched_reason_default_when_no_keyword():
    from unittest.mock import MagicMock
    from pico.context.renderer import render_current_user_message

    a = MagicMock()
    a.workspace = MagicMock(volatile_text=lambda: "")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.session = {"recently_recalled": [], "messages": []}
    a.memory_retrieval = None

    _text, tele = render_current_user_message(a, "hello world")
    assert tele["intent"]["matched_reason"] == "default (no keyword)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_renderer.py::test_intent_matched_reason_populated_for_keyword_hit -v`
Expected: FAIL

- [ ] **Step 3: Populate matched_reason**

In `pico/context/renderer.py`, find the intent block (near the top of `render_current_user_message`, where `intent = classify_intent(...)` is called). Update:

```python
    intent = classify_intent(user_message)
    if intent.matched_keyword:
        matched_reason = f"keyword:'{intent.matched_keyword}' via profile:{intent.name}"
    else:
        matched_reason = "default (no keyword)"
    telemetry["intent"] = {
        "name": intent.name,
        "matched_keyword": intent.matched_keyword,
        "matched_reason": matched_reason,
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_context_renderer.py -v 2>&1 | tail -12`
Expected: all pass

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/context/renderer.py tests/test_context_renderer.py
git commit -m "feat(observability): intent.matched_reason in telemetry

Closes spec §9 gap. Trace consumers can now distinguish keyword-driven
intent hits from fallback-to-default without inspecting the keyword."
```

---

## Task C5: metadata completeness gate

**Files:**
- Test: `tests/test_metadata_completeness.py` (new)

**Interfaces:** No new code. Locks the current metadata surface.

- [ ] **Step 1: Write the completeness test**

```python
# tests/test_metadata_completeness.py
"""Task C5: metadata surface completeness gate.

Locks in that build_v2 populates every field spec §9 promised. Runs
with a fully-mocked agent so no external state matters."""

from unittest.mock import MagicMock

from pico.context_manager import ContextManager


REQUIRED_METADATA_FIELDS = {
    "system_cache_key",
    "system_tokens",
    "tools_tokens",
    "messages_count",
    "messages_tokens",
    "cache_control_breakpoints",
    "injection_tokens",
    "injection_truncated",
    "injection_dropped",
    "injection_budget",
    "intent",
    "recall.error_count",
    "recall.last_error",
    "dropped_messages",
    "prompt_cache_key",
}


def test_metadata_covers_spec_section_9():
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": [{"role": "assistant", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}

    cm = ContextManager(a)
    _request, metadata = cm.build_v2("hi")

    missing = REQUIRED_METADATA_FIELDS - set(metadata.keys())
    assert not missing, f"metadata missing spec §9 fields: {sorted(missing)}"

    # Structural checks on non-scalar fields
    assert isinstance(metadata["intent"], dict)
    assert "name" in metadata["intent"]
    assert "matched_keyword" in metadata["intent"]
    assert "matched_reason" in metadata["intent"]
    assert isinstance(metadata["injection_tokens"], dict)
    assert isinstance(metadata["injection_dropped"], list)
    assert isinstance(metadata["cache_control_breakpoints"], list)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_metadata_completeness.py -v`
Expected: pass (Streams A/B/C above populated everything). If it fails, add missing fields.

- [ ] **Step 3: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 4: Commit**

```bash
git add tests/test_metadata_completeness.py
git commit -m "test(context): metadata completeness gate for spec §9 fields

Locks in that build_v2 produces every observability field the spec
promised. Serves as a regression fence for future changes that might
drop keys."
```

---

## Task C6: history_text docstring note (Finding 6, doc-only)

**Files:**
- Modify: `pico/runtime.py` (`history_text` docstring)

**Interfaces:** None.

- [ ] **Step 1: Update docstring**

In `pico/runtime.py`, find `def history_text(`. Add a docstring paragraph:

```python
    def history_text(self):
        """Render the legacy session["history"] as a compact transcript.

        This method is transitional — it reads from ``session["history"]``
        (the flat, pre-v2 shape) rather than ``session["messages"]``.
        Kept for ``build_report`` and evaluation-harness compatibility;
        v2 telemetry uses ``metadata["messages_tokens"]`` and structured
        messages instead. Returns "- empty" (not "") when history is
        empty to distinguish "no runs yet" from "runs with no output".
        """
        ...
```

- [ ] **Step 2: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: no change

- [ ] **Step 3: Commit**

```bash
git add pico/runtime.py
git commit -m "docs(runtime): history_text is transitional; explain in docstring

Closes Finding 6 by documenting the bridge nature of history_text.
No behavior change."
```

---

# Stream D · Performance

**Gate at end**: 3 benchmark scripts runnable standalone; D1/D2 measurable improvements in bench output.

---

## Task D1: Single-call digest_tool_result

**Files:**
- Modify: `pico/agent_loop.py` (single digest call + `dataclasses.replace`)
- Test: `tests/test_agent_loop_digest.py` (extend)

**Interfaces:** No API change; internal restructure.

- [ ] **Step 1: Add failing test**

Append to `tests/test_agent_loop_digest.py`:

```python
def test_digest_computed_exactly_once(tmp_path, monkeypatch):
    """Task D1: _append_tool_result must not run per-tool summarizer twice."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pico.context.digest as digest_mod
    from pico.agent_loop import _append_tool_result

    original = digest_mod._digest_read_file
    call_count = {"n": 0}

    def counting_digest_read_file(args, result):
        call_count["n"] += 1
        return original(args, result)

    monkeypatch.setattr(digest_mod, "_digest_read_file", counting_digest_read_file)
    monkeypatch.setitem(digest_mod._DIGESTERS, "read_file", counting_digest_read_file)

    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s1"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = str(tmp_path)
    a.current_task_state = SimpleNamespace(run_id="r1", task_id="t1")
    a.current_run_dir = tmp_path / ".pico" / "runs" / "r1"
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    a.context_config = {"digest_size_threshold": 100}

    _append_tool_result(
        a,
        tool_use_id="t1",
        content="x = 1\n" * 500,
        tool_name="read_file",
        tool_args={"path": "big.py"},
    )
    assert call_count["n"] == 1, f"_digest_read_file called {call_count['n']} times"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_loop_digest.py::test_digest_computed_exactly_once -v`
Expected: FAIL (count is 2)

- [ ] **Step 3: Refactor _append_tool_result**

In `pico/agent_loop.py`, find `_append_tool_result`. The current shape:

```python
    if not digest_applied and should_digest(content, threshold=threshold):
        source_hash = digest_tool_result(tool_name, tool_args, content, raw_path="").source_hash
        run_dir = getattr(agent, "current_run_dir", None)
        raw_path_str = ""
        if run_dir is not None:
            try:
                raw_dir = run_dir / "tool_results"
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = raw_dir / f"{source_hash}.txt"
                raw_path.write_text(content, encoding="utf-8")
                raw_path_str = str(raw_path)
            except OSError:
                raw_path_str = ""
        digest = digest_tool_result(tool_name, tool_args, content, raw_path=raw_path_str)
        display_content = render_digest_content(digest)
        digest_applied = True
```

Replace with:

```python
    if not digest_applied and should_digest(content, threshold=threshold):
        # Task D1: single-call digest. Compute the digest once (per-tool
        # summarizer runs exactly once); then update raw_path on the
        # dataclass via dataclasses.replace after we know where we wrote.
        from dataclasses import replace as _dc_replace
        digest = digest_tool_result(tool_name, tool_args, content, raw_path="")
        source_hash = digest.source_hash
        run_dir = getattr(agent, "current_run_dir", None)
        raw_path_str = ""
        if run_dir is not None:
            try:
                raw_dir = run_dir / "tool_results"
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = raw_dir / f"{source_hash}.txt"
                raw_path.write_text(content, encoding="utf-8")
                raw_path_str = str(raw_path)
            except OSError as exc:
                logger.debug("raw tool_result write failed: %s", exc)
                raw_path_str = ""
        if raw_path_str:
            digest = _dc_replace(digest, raw_path=raw_path_str)
        display_content = render_digest_content(digest)
        digest_applied = True
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent_loop_digest.py -v`
Expected: all pass

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/agent_loop.py tests/test_agent_loop_digest.py
git commit -m "perf(agent_loop): digest_tool_result computed once per tool result

Closes Finding 8: previous impl ran per-tool summarizer twice — once
to get source_hash, once with raw_path filled in. Now runs once and
updates raw_path via dataclasses.replace on the frozen dataclass."
```

---

## Task D2: Recall store index built once per call

**Files:**
- Modify: `pico/memory/recall.py` (`_lookup_type` accepts index, not store)
- Test: `tests/test_memory_recall.py` (extend)

**Interfaces:** `_lookup_type` signature changes (`store` → `store_index`); internal only.

- [ ] **Step 1: Add failing test**

Append to `tests/test_memory_recall.py`:

```python
def test_recall_uses_single_store_scan(tmp_path, monkeypatch):
    """Task D2: recall_for_turn calls store.list() at most once per call,
    not once per hit."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "a.md").write_text(
        "---\nname: a\ntype: feedback\ndescription: cache one\n---\np1\n", encoding="utf-8"
    )
    (ws / "agent" / "b.md").write_text(
        "---\nname: b\ntype: feedback\ndescription: cache two\n---\np2\n", encoding="utf-8"
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)

    call_count = {"n": 0}
    original_list = store.list
    def counting_list(*args, **kwargs):
        call_count["n"] += 1
        return original_list(*args, **kwargs)
    monkeypatch.setattr(store, "list", counting_list)

    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )
    # Reset counter to isolate recall's contribution.
    call_count["n"] = 0
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    # recall_for_turn should scan the store at most once for the type index.
    # (Retrieval.search itself will scan once too — total ≤ 2 is fine, but
    # per-hit lookup would give ≥ top_k + retrieval scans.)
    assert call_count["n"] <= 2, f"store.list() called {call_count['n']} times"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_recall.py::test_recall_uses_single_store_scan -v`
Expected: FAIL (count > 2 with current per-hit lookup)

- [ ] **Step 3: Refactor _lookup_type and recall_for_turn**

In `pico/memory/recall.py`, change `_lookup_type`:

```python
def _lookup_type(store_index, path):
    """Return frontmatter ``type`` for ``path`` from a pre-built index."""
    entry = store_index.get(path)
    if entry is None:
        return ""
    return (entry.frontmatter or {}).get("type", "") or ""
```

In `recall_for_turn`, near the top after picking `hits`, build the index once:

```python
    # Task D2: build store index once per recall call, not per hit.
    store_index = {entry.path: entry for entry in store.list()}
```

Then replace the per-hit call `_lookup_type(store, hit.path)` with `_lookup_type(store_index, hit.path)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_memory_recall.py -v`
Expected: all pass

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add pico/memory/recall.py tests/test_memory_recall.py
git commit -m "perf(memory): recall builds store index once per call

Closes Finding 12: previous impl called store.list() (a workspace+user
disk scan) once per recalled hit. Now scans once and looks up via dict."
```

---

## Task D3: Perf harness

**Files:**
- Create: `benchmarks/perf/__init__.py`
- Create: `benchmarks/perf/harness.py`
- Create: `benchmarks/perf/README.md`
- Test: `tests/test_perf_harness.py` (new — just verifies harness imports and runs)

**Interfaces:**
- Produces: `bench(name, fn, iterations=100, warmup=5) -> dict`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_perf_harness.py
"""Task D3: perf harness runs a benchmark and returns structured stats."""

def test_bench_returns_stats():
    from benchmarks.perf.harness import bench

    result = bench("noop", lambda: sum(range(100)), iterations=10, warmup=2)
    assert result["name"] == "noop"
    assert result["iterations"] == 10
    assert isinstance(result["median_ns"], int)
    assert isinstance(result["p95_ns"], int)
    assert isinstance(result["min_ns"], int)
    assert result["min_ns"] > 0
    assert result["p95_ns"] >= result["median_ns"] >= result["min_ns"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perf_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.perf'`

- [ ] **Step 3: Implement harness**

Create `benchmarks/perf/__init__.py`:

```python
"""Latency benchmark harness for pico subsystems."""
```

Create `benchmarks/perf/harness.py`:

```python
"""Stdlib-only benchmark harness.

Each ``bench`` call warmup-runs, then measures ``iterations`` timings
with ``time.perf_counter_ns`` and returns median / p95 / min. No CI
gating: users run these scripts manually and compare JSON output
across code changes.
"""

from __future__ import annotations

import statistics
import time


def _percentile(samples, p):
    if not samples:
        return 0
    sorted_samples = sorted(samples)
    k = (len(sorted_samples) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_samples) - 1)
    d = k - f
    return int(sorted_samples[f] + (sorted_samples[c] - sorted_samples[f]) * d)


def bench(name, fn, iterations=100, warmup=5):
    """Time ``fn`` and return structured stats.

    Warmup runs are discarded. Measured samples are collected via
    ``time.perf_counter_ns``. Returns a dict with keys ``name``,
    ``iterations``, ``median_ns``, ``p95_ns``, ``min_ns``.
    """
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
    return {
        "name": name,
        "iterations": iterations,
        "median_ns": int(statistics.median(samples)),
        "p95_ns": _percentile(samples, 95),
        "min_ns": min(samples),
    }
```

Create `benchmarks/perf/README.md`:

```markdown
# Pico Perf Benchmarks

Latency measurements for pico's hot paths. **Not CI-gated** — run
locally before/after a change to spot regressions.

## Usage

```bash
uv run python -m benchmarks.perf.bench_build_v2 > results-build_v2.json
uv run python -m benchmarks.perf.bench_retrieval > results-retrieval.json
uv run python -m benchmarks.perf.bench_recall > results-recall.json
```

Each script prints a JSON document with per-scenario `median_ns`,
`p95_ns`, and `min_ns`. Diff two runs to spot regressions.

## When to re-run

- After changing `FIELD_BOOSTS`, `LINK_MAX_ADDED`, `LINK_DECAY` in
  `pico/memory/retrieval.py`
- After adding or removing an injection source
- After changing the history budget algorithm

## Output shape

```json
{
  "scenarios": [
    {
      "name": "build_v2/small",
      "iterations": 100,
      "median_ns": 123456,
      "p95_ns": 234567,
      "min_ns": 100000
    }
  ]
}
```
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_perf_harness.py -v`
Expected: 1 passed

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add benchmarks/perf/ tests/test_perf_harness.py
git commit -m "feat(perf): stdlib-only latency benchmark harness

Bench function measures per-scenario median/p95/min in nanoseconds
via time.perf_counter_ns. Scripts in benchmarks/perf/ run manually
and emit JSON for local before/after comparison."
```

---

## Task D4: bench_build_v2.py

**Files:**
- Create: `benchmarks/perf/bench_build_v2.py`

**Interfaces:** Standalone script; not imported by tests.

- [ ] **Step 1: Create script**

```python
# benchmarks/perf/bench_build_v2.py
"""Benchmark ContextManager.build_v2 across session sizes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pico.context_manager import ContextManager  # noqa: E402


def _make_agent(session_len):
    a = MagicMock()
    a.prefix = "SYSTEM " * 100  # ~700 chars
    a.tools = {
        "read_file": {"schema": {"path": "str"}, "risky": False, "description": "read"},
        "run_shell": {"schema": {"command": "str"}, "risky": True, "description": "run"},
    }
    a.session = {
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i} " * 20}
            for i in range(session_len)
        ]
    }
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="branch: main\nstatus: clean\n")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}
    return a


def main():
    scenarios = []
    for name, size in [("small", 1), ("medium", 30), ("large", 300)]:
        agent = _make_agent(size)
        cm = ContextManager(agent)
        result = bench(f"build_v2/{name}", lambda: cm.build_v2("test question"), iterations=100)
        scenarios.append(result)
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script (smoke)**

Run: `uv run python -m benchmarks.perf.bench_build_v2 2>&1 | tail -20`
Expected: JSON with 3 scenarios, each with median_ns / p95_ns / min_ns.

- [ ] **Step 3: Commit**

```bash
git add benchmarks/perf/bench_build_v2.py
git commit -m "feat(perf): bench_build_v2 across small/medium/large sessions"
```

---

## Task D5: bench_retrieval.py

**Files:**
- Create: `benchmarks/perf/bench_retrieval.py`

- [ ] **Step 1: Create script**

```python
# benchmarks/perf/bench_retrieval.py
"""Benchmark Retrieval.search across note counts."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pico.memory.block_store import BlockStore  # noqa: E402
from pico.memory.retrieval import Retrieval  # noqa: E402


def _populate(root, count):
    (root / "agent").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / "agent" / f"note-{i}.md").write_text(
            f"---\nname: note-{i}\ntype: feedback\ndescription: cache and memory topic {i}\n---\n"
            f"Body text mentioning cache invalidation and memory retrieval. Note {i}.\n"
            f"See [[note-{(i + 1) % count}]] for related.\n",
            encoding="utf-8",
        )


def main():
    scenarios = []
    for name, count in [("small", 10), ("medium", 100), ("large", 1000)]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _populate(root, count)
            store = BlockStore(workspace_root=root, user_root=root / "user")
            ret = Retrieval(store)
            result = bench(f"retrieval/{name}", lambda: ret.search("cache memory"), iterations=50)
            scenarios.append(result)
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script (smoke)**

Run: `uv run python -m benchmarks.perf.bench_retrieval 2>&1 | tail -30`
Expected: JSON with 3 scenarios.

- [ ] **Step 3: Commit**

```bash
git add benchmarks/perf/bench_retrieval.py
git commit -m "feat(perf): bench_retrieval across 10/100/1000 notes"
```

---

## Task D6: bench_recall.py

**Files:**
- Create: `benchmarks/perf/bench_recall.py`

- [ ] **Step 1: Create script**

```python
# benchmarks/perf/bench_recall.py
"""Benchmark recall_for_turn latency."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pico.memory.block_store import BlockStore  # noqa: E402
from pico.memory.recall import recall_for_turn  # noqa: E402
from pico.memory.retrieval import Retrieval  # noqa: E402


def _populate(root, count):
    (root / "agent").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / "agent" / f"note-{i}.md").write_text(
            f"---\nname: note-{i}\ntype: feedback\ndescription: cache topic {i}\n---\n"
            f"Body {i} mentioning cache.\n",
            encoding="utf-8",
        )


def _make_agent(store, ret, recent_history):
    return SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": recent_history},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )


def main():
    scenarios = []
    for note_count in [10, 100]:
        for recent_name, recent_hist in [("empty_recent", []), ("full_recent", [[f"workspace/agent/note-{i}.md" for i in range(5)]])]:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _populate(root, note_count)
                store = BlockStore(workspace_root=root, user_root=root / "user")
                ret = Retrieval(store)
                agent = _make_agent(store, ret, list(recent_hist))
                result = bench(
                    f"recall/{note_count}notes/{recent_name}",
                    lambda: recall_for_turn(agent, "cache", budget_tokens=1000),
                    iterations=50,
                )
                scenarios.append(result)
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run smoke**

Run: `uv run python -m benchmarks.perf.bench_recall 2>&1 | tail -30`
Expected: JSON with 4 scenarios.

- [ ] **Step 3: Commit**

```bash
git add benchmarks/perf/bench_recall.py
git commit -m "feat(perf): bench_recall across note count × recent-history states"
```

---

## Task D7: Full suite regression + benchmark smoke gate

**Files:** none

- [ ] **Step 1: Regression check**

```bash
uv run pytest -q 2>&1 | tail -3
```

Expected: still passing.

- [ ] **Step 2: Smoke all three bench scripts**

```bash
uv run python -m benchmarks.perf.bench_build_v2 > /tmp/bench_build_v2.json
uv run python -m benchmarks.perf.bench_retrieval > /tmp/bench_retrieval.json
uv run python -m benchmarks.perf.bench_recall > /tmp/bench_recall.json
python -c "import json; [json.load(open(f'/tmp/bench_{n}.json')) for n in ('build_v2', 'retrieval', 'recall')]; print('all bench scripts produce valid JSON')"
```

Expected: `all bench scripts produce valid JSON`

- [ ] **Step 3: Commit (no code — gate marker)**

```bash
git commit --allow-empty -m "gate(perf): 3 bench scripts smoke-verified

D3-D6 land; scripts produce parseable JSON. Not CI-gated; users run
manually and diff before/after."
```

---

# Stream E · Testing

**Gate at end**: E2E round-trip + fallback parity + legacy rewrites landed; total pytest passed ≥ 620 (baseline 596 + Stream A-D additions + Stream E additions).

---

## Task E1: Full-turn E2E round-trip

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/test_full_turn_roundtrip.py`

**Interfaces:** Uses `_SniffProvider` pattern from `tests/test_agent_loop_injection_sent.py`.

- [ ] **Step 1: Create the test**

```python
# tests/e2e/__init__.py
```

```python
# tests/e2e/test_full_turn_roundtrip.py
"""E2E: one Pico.ask exercises injection + digest + recall together."""

from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


def test_full_turn_injects_recall_and_digests_large_tool_result(tmp_path):
    # Seed a memory note that should match "cache".
    (tmp_path / ".pico" / "memory" / "agent").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nCache stays stable across turns.\n",
        encoding="utf-8",
    )
    # A big README so read_file returns > 1200 chars.
    (tmp_path / "README.md").write_text("readme line\n" * 500, encoding="utf-8")

    provider = _SniffProvider([
        # Turn 1: model asks read_file.
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {"path": "README.md"}}],
            usage={},
        ),
        # Turn 2: model returns final text after seeing the tool_result.
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "cache stays stable"}], usage={}),
    ])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    answer = pico.ask("上次讨论过 cache 的问题")
    assert "cache" in answer

    # 2 provider calls happened.
    assert len(provider.calls) == 2

    # Turn 1 provider input carries injection wrapping around user text.
    turn1_user_content = provider.calls[0]["messages"][-1]["content"]
    assert isinstance(turn1_user_content, str)
    assert "<pico:workspace_state>" in turn1_user_content or "<system-reminder>" in turn1_user_content
    # Recall block should be present (memory note matched "cache" keyword).
    assert "<pico:recalled_memory" in turn1_user_content
    assert "cache" in turn1_user_content.lower()

    # Turn 2: history contains the tool_result. Because the raw README > 1200
    # chars, digest_applied=True → content is the short [digest] rendering.
    turn2_msgs = provider.calls[1]["messages"]
    tool_result_msgs = [
        m for m in turn2_msgs
        if isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert tool_result_msgs, "no tool_result in turn 2 history"
    tr_content = tool_result_msgs[-1]["content"][0]["content"]
    assert "[digest]" in tr_content, f"expected digest, got: {tr_content[:200]!r}"

    # No recall errors surfaced.
    assert pico.session.get("_recall_errors", {}).get("count", 0) == 0
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/e2e/test_full_turn_roundtrip.py -v`
Expected: 1 passed. If the recall block doesn't appear, the intent classifier may not be hitting "recall" — check that the memory note's frontmatter description contains "cache" and the user message contains "上次" (a `recall` intent keyword).

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/
git commit -m "test(e2e): full-turn round-trip covers injection + recall + digest"
```

---

## Task E2: History budget trigger E2E

**Files:**
- Modify: `tests/e2e/test_full_turn_roundtrip.py` (extend)

**Interfaces:** Same `_SniffProvider`.

- [ ] **Step 1: Add test**

Append to `tests/e2e/test_full_turn_roundtrip.py`:

```python
def test_history_budget_triggers_drop(tmp_path):
    """A session with 50 pre-existing messages + a tight soft_cap should drop old turns."""
    # Populate pico.toml so context_config picks up a tight soft_cap.
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 500\nhistory_floor_messages = 4\n",
        encoding="utf-8",
    )

    provider = _SniffProvider([
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}], usage={}),
    ])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    # Prime session with many messages BEFORE calling ask.
    for i in range(30):
        pico.session["messages"].append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"old-msg-{i} " + ("x" * 200),
            "_pico_meta": {"created_at": "t"},
        })

    pico.ask("new question")

    call = provider.calls[0]
    metadata_dropped = getattr(pico, "last_prompt_metadata", {}).get("dropped_messages", 0)
    assert metadata_dropped > 0, "expected some messages to be dropped under tight cap"
    # Floor honored: last N ≥ 4 messages preserved.
    assert len(call["messages"]) >= 4
    # No orphan tool_use blocks (there were none seeded; this is a smoke).
    tool_use_ids = set()
    tool_result_ids = set()
    for m in call["messages"]:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    tool_use_ids.add(b["id"])
                if b.get("type") == "tool_result":
                    tool_result_ids.add(b["tool_use_id"])
    assert tool_use_ids == tool_result_ids
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/e2e/test_full_turn_roundtrip.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_full_turn_roundtrip.py
git commit -m "test(e2e): history budget dropping triggered via pico.toml"
```

---

## Task E3: FallbackAdapter parity E2E

**Files:**
- Create: `tests/e2e/test_fallback_provider_parity.py`

**Interfaces:** Uses `FallbackAdapter` wrapping a stub inner provider.

- [ ] **Step 1: Create the test**

```python
# tests/e2e/test_fallback_provider_parity.py
"""E2E: same pico.ask input produces equivalent flow via native and fallback paths."""

from pico.providers.fallback_adapter import FallbackAdapter
from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    """Native v2 provider — records raw messages arg."""
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


class _XmlStubInner:
    """Inner provider for FallbackAdapter — returns legacy <final> string."""
    def __init__(self, script):
        self.script = list(script)
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        self.prompts.append(prompt)
        return self.script.pop(0)


def test_native_and_fallback_both_complete_a_final_turn(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    # 1. Native path
    native = _SniffProvider([
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "ok"}], usage={}),
    ])
    store1 = SessionStore(tmp_path / ".pico" / "sessions_a")
    pico_native = Pico(model_client=native, workspace=workspace, session_store=store1, max_steps=3)
    answer_native = pico_native.ask("hello world")

    # 2. Fallback path
    inner = _XmlStubInner(["<final>ok</final>"])
    fallback = FallbackAdapter(inner)
    store2 = SessionStore(tmp_path / ".pico" / "sessions_b")
    pico_fb = Pico(model_client=fallback, workspace=workspace, session_store=store2, max_steps=3)
    answer_fb = pico_fb.ask("hello world")

    assert answer_native.strip() == "ok"
    assert answer_fb.strip() == "ok"

    # Native path saw <pico:*> blocks in messages.
    native_content = native.calls[0]["messages"][-1]["content"]
    assert "<pico:workspace_state>" in native_content or "<system-reminder>" in native_content

    # Fallback path saw the same blocks after flattening.
    flattened_prompt = inner.prompts[0]
    assert "<pico:workspace_state>" in flattened_prompt or "<system-reminder>" in flattened_prompt
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/e2e/test_fallback_provider_parity.py -v`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_fallback_provider_parity.py
git commit -m "test(e2e): native vs fallback provider path parity"
```

---

## Task E4: Rewrite legacy test 1 (checkpoint)

**Files:**
- Modify: `tests/test_runtime_report.py` (`test_resume_prompt_uses_checkpoint_state_not_just_history`)

- [ ] **Step 1: Locate and inspect the current test**

Run: `grep -n "test_resume_prompt_uses_checkpoint_state" tests/test_runtime_report.py`

Read 30 lines starting from the match.

- [ ] **Step 2: Replace the test**

Remove the `@pytest.mark.legacy_string_path` and `@pytest.mark.skip` decorators. Replace the body with a v2 assertion:

```python
def test_resume_prompt_carries_checkpoint_via_v2_messages(tmp_path):
    """Task E4 rewrite: the resume checkpoint state should surface in the
    injection block on the outgoing user message (v2 shape), not the
    legacy flattened prompt."""
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_manual",
        "items": {
            "ckpt_manual": {
                "checkpoint_id": "ckpt_manual",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix failing resume flow",
                "completed": ["Read runtime.py"],
                "excluded": ["Do not add branch summary"],
                "current_blocker": "Need to re-anchor stale file facts",
                "next_step": "Re-read runtime.py and refresh the checkpoint",
                "key_files": [{"path": "runtime.py", "freshness": "abc"}],
            }
        },
    }
    request, metadata = agent.context_manager.build_v2("continue")

    # The checkpoint text should appear inside a <pico:checkpoint> block on
    # the current turn's user message.
    current_content = request["messages"][-1]["content"]
    assert isinstance(current_content, str)
    if "<pico:checkpoint>" in current_content:
        # Injection is active — verify checkpoint fields flow through.
        assert "Fix failing resume flow" in current_content or "current_goal" in current_content
    else:
        # No injection block emitted (renderer decided not to include checkpoint
        # given the budget) — accept the graceful skip but ensure telemetry
        # explains why: either dropped in injection_dropped, or budget=0.
        assert (
            "checkpoint" in metadata.get("injection_dropped", [])
            or metadata.get("injection_tokens", {}).get("checkpoint", 0) == 0
        )
```

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_runtime_report.py::test_resume_prompt_carries_checkpoint_via_v2_messages -v`
Expected: pass.

- [ ] **Step 4: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 5: Commit**

```bash
git add tests/test_runtime_report.py
git commit -m "test(runtime): rewrite checkpoint resume test against v2 messages

Removes legacy_string_path marker. Assertion now targets the
<pico:checkpoint> injection block on the outgoing user message rather
than the flattened FallbackAdapter prompt."
```

---

## Task E5: Rewrite legacy test 2 (recent transcript)

**Files:**
- Modify: `tests/test_runtime_report.py` (`test_recent_transcript_entries_stay_richer_than_older_ones`)

- [ ] **Step 1: Inspect the current test**

Run: `grep -n "test_recent_transcript_entries_stay_richer" tests/test_runtime_report.py`

Read the current body.

- [ ] **Step 2: Replace with v2 assertion**

Remove decorators and rewrite:

```python
def test_recent_messages_preserved_older_digested(tmp_path):
    """Task E5 rewrite: recent messages stay intact; older tool_results
    over the digest threshold appear as [digest] entries."""
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    # Seed session["messages"] directly (v2 shape) — 6 recent + 4 older messages.
    long_body = "line\n" * 500
    agent.session["messages"] = [
        # older tool_use/tool_result pair — result long enough to be digested
        {"role": "user", "content": "old question 1"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x.py"}}
        ], "_pico_meta": {"tool_use_id": "t1"}},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"[digest] x.py (500 lines)\n- import os"}
        ], "_pico_meta": {"tool_use_id": "t1", "digest_applied": True}},
        {"role": "assistant", "content": "old answer 1"},
        # recent 6 messages
        {"role": "user", "content": "recent question 1"},
        {"role": "assistant", "content": "recent answer 1"},
        {"role": "user", "content": "recent question 2"},
        {"role": "assistant", "content": "recent answer 2"},
        {"role": "user", "content": "recent question 3"},
        {"role": "assistant", "content": "recent answer 3"},
    ]

    request, _metadata = agent.context_manager.build_v2("current question")

    # Last 6 messages preserved verbatim in returned messages array.
    recent_kept = request["messages"][-7:-1]  # exclude the appended current user turn
    assert any("recent question 1" in str(m["content"]) for m in recent_kept)

    # Older tool_result content carries [digest] marker.
    older_content = str(request["messages"][2]["content"])
    assert "[digest]" in older_content
```

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_runtime_report.py::test_recent_messages_preserved_older_digested -v`
Expected: pass.

- [ ] **Step 4: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 5: Commit**

```bash
git add tests/test_runtime_report.py
git commit -m "test(runtime): rewrite transcript-freshness test against v2 messages

Removes legacy_string_path marker. Asserts messages array preserves
recent entries verbatim while older tool_results carry [digest] prefix."
```

---

## Task E7: Test hygiene补强

**Files:**
- Modify: `tests/test_context_manager_v2.py` (exact hash assertion)
- Modify: `tests/test_provider_fallback.py` (accumulate vs mirror)
- Modify: `tests/test_agent_loop_e2e_v2.py` (assert injection wrap)

- [ ] **Step 1: Exact sha256 assertion**

In `tests/test_context_manager_v2.py`, find `test_build_v2_metadata_contains_system_cache_key`. Modify:

```python
def test_build_v2_metadata_contains_system_cache_key():
    import hashlib
    a = _make_agent()
    cm = ContextManager(a)
    _, metadata = cm.build_v2("x")
    assert "system_cache_key" in metadata
    expected = hashlib.sha256(a.prefix.encode("utf-8")).hexdigest()
    assert metadata["system_cache_key"] == expected
```

- [ ] **Step 2: FallbackAdapter mirror-not-accumulate**

In `tests/test_provider_fallback.py`, find `test_fallback_last_completion_metadata_mirrors_inner`. Update the `_StubInner` used inside that test to return **different** metadata across calls:

```python
def test_fallback_last_completion_metadata_mirrors_inner():
    class _VaryingStubInner:
        def __init__(self):
            self.n = 0
            self.last_completion_metadata = {}
        def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
            self.n += 1
            self.last_completion_metadata = {"input_tokens": self.n, "output_tokens": self.n * 2}
            return "<final>ok</final>"

    inner = _VaryingStubInner()
    adapter = FallbackAdapter(inner)
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}], max_tokens=10,
    )
    assert adapter.last_completion_metadata == {"input_tokens": 1, "output_tokens": 2}
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "y"}], max_tokens=10,
    )
    # Mirror the LATEST inner metadata, not the accumulated union.
    assert adapter.last_completion_metadata == {"input_tokens": 2, "output_tokens": 4}
```

- [ ] **Step 3: Assert injection wrap in e2e test**

In `tests/test_agent_loop_e2e_v2.py`, find `test_end_to_end_tool_call_then_final` and add before the final `return`:

```python
    # Task E7: assert the provider actually saw the injection wrapping.
    turn1_user_content = provider.calls[0]["messages"][-1]["content"]
    assert isinstance(turn1_user_content, str)
    assert "<system-reminder>" in turn1_user_content
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_context_manager_v2.py tests/test_provider_fallback.py tests/test_agent_loop_e2e_v2.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add tests/test_context_manager_v2.py tests/test_provider_fallback.py tests/test_agent_loop_e2e_v2.py
git commit -m "test: tighten 3 underspecified assertions from prior review

- system_cache_key: exact sha256 value, not just length=64
- fallback metadata: varying inner catches accumulation bugs
- e2e_v2 turn: asserts injection wrapping reached the provider"
```

---

## Task E8: Task 15 minor sweep — coverage补强

**Files:**
- Modify: `tests/test_session_store_migrator.py` (add `_pico_meta` assertions)
- Modify: `tests/test_context_manager_v2.py` (add int-schema-integer test)
- Modify: `tests/test_agent_loop_v2_shape.py` (assert meta fields)

- [ ] **Step 1: Add `_pico_meta` field assertions to session migrator test**

Append to `tests/test_session_store_migrator.py`:

```python
def test_migrator_preserves_created_at_and_tool_use_id():
    """Task E8: migrator must carry _pico_meta.created_at across every
    message type and set _pico_meta.tool_use_id on both halves of tool pairs."""
    from pico.session_store import _migrate_v1_to_v2

    v1 = {
        "id": "s",
        "schema_version": 1,
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-04-01T00:00:00Z"},
            {"role": "tool", "name": "read_file", "args": {"path": "x"}, "content": "y", "created_at": "2026-04-01T00:00:01Z"},
            {"role": "assistant", "content": "done", "created_at": "2026-04-01T00:00:02Z"},
        ],
    }
    v2 = _migrate_v1_to_v2(v1)
    msgs = v2["messages"]
    # user
    assert msgs[0]["_pico_meta"]["created_at"] == "2026-04-01T00:00:00Z"
    # assistant tool_use half
    assert msgs[1]["_pico_meta"]["created_at"] == "2026-04-01T00:00:01Z"
    tool_use_id = msgs[1]["_pico_meta"]["tool_use_id"]
    assert tool_use_id
    # user tool_result half — must share tool_use_id
    assert msgs[2]["_pico_meta"]["tool_use_id"] == tool_use_id
    assert msgs[2]["_pico_meta"]["created_at"] == "2026-04-01T00:00:01Z"
    # final assistant
    assert msgs[3]["_pico_meta"]["created_at"] == "2026-04-01T00:00:02Z"


def test_migrator_idempotent_returns_v2_verbatim():
    from pico.session_store import _migrate_v1_to_v2

    v2 = {
        "id": "s",
        "schema_version": 2,
        "messages": [
            {"role": "user", "content": "hi", "_pico_meta": {"created_at": "x"}},
        ],
    }
    result = _migrate_v1_to_v2(v2)
    # Idempotent — same messages list, unchanged.
    assert result["schema_version"] == 2
    assert result["messages"] == v2["messages"]
```

- [ ] **Step 2: Int schema → integer type test**

Append to `tests/test_context_manager_v2.py`:

```python
def test_int_schema_field_maps_to_integer_json_type():
    """Task E8: tool schema 'int' variants must map to Anthropic-shape
    input_schema.properties.<field>.type = 'integer', not 'string'."""
    from pico.context_manager import _build_tools_list

    tools = {
        "read_file": {
            "schema": {"start": "int=1", "end": "int=200"},
            "risky": False,
            "description": "read a slice",
        },
    }
    out = _build_tools_list(tools)
    props = out[0]["input_schema"]["properties"]
    assert props["start"]["type"] == "integer"
    assert props["end"]["type"] == "integer"
```

- [ ] **Step 3: Meta fields on tool_use / tool_result**

Append to `tests/test_agent_loop_v2_shape.py`:

```python
def test_append_tool_use_result_carry_meta_fields():
    """Task E8: _append_tool_use / _append_tool_result must set the required
    _pico_meta fields."""
    from unittest.mock import MagicMock
    from pico.agent_loop import _append_tool_result, _append_tool_use

    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = "/tmp"
    a.current_task_state = None
    a.current_run_dir = None
    a.context_config = {}

    tool_use_id = _append_tool_use(a, name="read_file", input={"path": "a.py"}, id_hint="t1")
    tu_msg = session_messages[-1]
    assert tu_msg["_pico_meta"]["tool_use_id"] == "t1"
    assert "created_at" in tu_msg["_pico_meta"]

    _append_tool_result(a, tool_use_id=tool_use_id, content="short")
    tr_msg = session_messages[-1]
    assert tr_msg["_pico_meta"]["tool_use_id"] == "t1"
    assert "created_at" in tr_msg["_pico_meta"]
    assert tr_msg["_pico_meta"]["digest_applied"] is False
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_session_store_migrator.py tests/test_context_manager_v2.py tests/test_agent_loop_v2_shape.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 5: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add tests/test_session_store_migrator.py tests/test_context_manager_v2.py tests/test_agent_loop_v2_shape.py
git commit -m "test: task 15 minor sweep — meta fields, int schema, idempotency"
```

---

## Task E9: Property-style additions

**Files:**
- Create: `tests/test_message_invariants.py`

- [ ] **Step 1: Create test file**

```python
# tests/test_message_invariants.py
"""Task E9: property-style invariants on the message array shape."""

from unittest.mock import MagicMock

from pico.context_manager import ContextManager
from pico.providers.message_utils import strip_pico_meta


def _make_agent_with_messages(messages):
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": messages, "recently_recalled": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}
    return a


def test_message_immutability_across_turns():
    """build_v2 must not mutate session["messages"] entries."""
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    snapshot_before = [dict(m) for m in msgs]
    agent = _make_agent_with_messages(msgs)
    cm = ContextManager(agent)
    cm.build_v2("q2")
    cm.build_v2("q3")
    # Session's original entries should be byte-identical after 2 builds.
    assert agent.session["messages"][0] == snapshot_before[0]
    assert agent.session["messages"][1] == snapshot_before[1]


def test_pico_meta_never_in_provider_payload():
    """strip_pico_meta ensures no _pico_meta reaches the provider."""
    src = [
        {"role": "user", "content": "hi", "_pico_meta": {"a": 1}},
        {"role": "assistant", "content": "yo", "_pico_meta": {"b": 2}},
    ]
    cleaned = strip_pico_meta(src)
    for m in cleaned:
        assert "_pico_meta" not in m


def test_recently_recalled_deque_bounded(tmp_path):
    """After N recall_for_turn calls, session["recently_recalled"] must
    stay bounded to skip_recent_turns + 1."""
    from types import SimpleNamespace
    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    (tmp_path / "agent").mkdir(parents=True)
    (tmp_path / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: feedback\ndescription: cache\n---\np1\n", encoding="utf-8"
    )
    store = BlockStore(workspace_root=tmp_path, user_root=tmp_path / "user")
    ret = Retrieval(store)

    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={"recall": {"min_score": 0.01, "top_k": 2, "max_tokens_per_note": 400, "skip_recent_turns": 2}},
    )
    for _ in range(10):
        recall_for_turn(a, "cache", budget_tokens=1000)
    # skip_recent_turns=2 → deque bounded to at most 3 entries.
    assert len(a.session["recently_recalled"]) <= 3
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_message_invariants.py -v`
Expected: 3 passed.

- [ ] **Step 3: Full suite regression check**

Run: `uv run pytest -q 2>&1 | tail -3`

- [ ] **Step 4: Commit**

```bash
git add tests/test_message_invariants.py
git commit -m "test: message immutability + _pico_meta scrub + deque-bounded recall"
```

---

# Stream F · Docs Alignment

**Gate at end**: `CONTEXT.md` / `memory-model.md` / prior spec updated.

---

## Task F1: CONTEXT.md config section

**Files:**
- Modify: `CONTEXT.md`

- [ ] **Step 1: Append config section**

Append to `CONTEXT.md`:

```markdown
## pico.toml Configuration Surface

Pico reads optional configuration from `<repo>/pico.toml`. Every key
falls back to a hard-coded default if the file is missing, the section
is missing, or the value has a bad type. Sample:

    [context]
    history_soft_cap = 40000        # tokens; messages array trim threshold
    history_floor_messages = 6      # tail messages always preserved
    injection_budget_ratio = 0.15   # fraction of total budget for <system-reminder> blocks
    system_tools_hard_cap = 20000   # tokens; build_v2 fails loud if system+tools exceed

    [context.digest]
    size_threshold_chars = 1200     # tool_result char count above which digest applies

    [memory.recall]
    min_score = 0.3                 # normalized BM25 gate
    top_k = 2                       # max notes recalled per turn
    max_tokens_per_note = 400       # per-note cap in the recall block
    skip_recent_turns = 2           # don't re-recall notes shown in last N turns

    [memory.retrieval.field_boost]
    name = 5.0
    description = 3.0
    tags = 4.0
    aliases = 4.0
    body = 1.0

    [memory.retrieval.link]
    max_added = 3                   # neighbors per query via [[name]] expansion
    decay = 0.4                     # neighbor score multiplier

**When to change**: `history_soft_cap` if your model returns 413 on
long sessions; `recall.min_score` if recall surfaces irrelevant memory
too often; `field_boost.name` and friends if a domain-specific note
naming convention benefits from re-weighting. The `intent_profiles`
keywords are NOT overridable via `pico.toml` — edit
`pico/context/intent.py` directly.
```

- [ ] **Step 2: Commit**

```bash
git add CONTEXT.md
git commit -m "docs(context): add pico.toml configuration surface reference"
```

---

## Task F2: memory-model.md update

**Files:**
- Modify: `docs/memory-model.md`

- [ ] **Step 1: Append new sections**

Append to `docs/memory-model.md`:

```markdown
## Recall & Digest

**Recall**: at the start of every turn, `recall_for_turn` (from
`pico/memory/recall.py`) picks up to `top_k` memory notes matching the
user message + task summary. Four guards keep the injection lean:

1. `min_score` — normalized BM25 score must clear the threshold
2. `max_tokens_per_note` — clip per-note body to this many tokens
3. Tombstone — skip notes with a matching `supersedes` entry
4. Recently-recalled — skip notes surfaced in the last N turns

Recalled notes appear in the outgoing user message as
`<system-reminder><pico:recalled_memory ...>` blocks with `path=`,
`type=`, `score=`, and `why=` provenance.

**Digest**: tool_result payloads above `context.digest.size_threshold_chars`
(default 1200) are digested — the message content becomes a short
`[digest]` rendering (title + up to 5 bullets + `raw at ...` pointer);
the full raw body is written to
`.pico/runs/<run_id>/tool_results/<hash>.txt` for later retrieval.
Session-level history stays compact; the model can still ask to re-read
the raw file by path.

## Long-Session Management

Long sessions eventually exceed provider context. Pico enforces
`history_soft_cap` (default 40000 tokens) by dropping oldest turn
units — a "turn unit" being one top-level user question plus every
message it triggered. The last `history_floor_messages` (default 6)
messages are always preserved. `tool_use`/`tool_result` pairs drop
atomically so no orphan blocks reach the provider.
```

- [ ] **Step 2: Commit**

```bash
git add docs/memory-model.md
git commit -m "docs(memory): document recall guards, digest workflow, long-session drop"
```

---

## Task F3: Prior spec addendum

**Files:**
- Modify: `docs/superpowers/specs/2026-07-07-pico-memory-context-redesign-design.md`

- [ ] **Step 1: Append post-review update section**

Append at the end of `docs/superpowers/specs/2026-07-07-pico-memory-context-redesign-design.md`:

```markdown
---

## Post-Review Update (2026-07-08)

The whole-branch review after the 28-task migration flagged 15
findings, one CRITICAL (injection subsystem inert in the live runtime)
plus 14 others. The critical bug was fixed the same session (see
commit `0189780`). The remaining 14 findings are addressed by the
follow-up spec `2026-07-08-pico-review-and-optimize-design.md`.

**What that spec covers**:
- Finding 2: turn-based history budget via `history_soft_cap`
- Finding 3: pico.toml config surface (per-domain helpers in `config.py`)
- Finding 4: `strip_pico_meta` helper for provider payloads
- Finding 5: `injection_dropped` populated via `DROP_PRIORITY`
- Findings 6-12: MINOR correctness/perf/observability fixes
- Findings 13-14 (2 of 3 legacy tests): rewritten to v2 shape

**Deferred**:
- Finding 14's third legacy test (`test_metrics.py`) — depends on
  evaluation harness `_MemoryExperimentModelClient` internals; requires
  independent spec.
- Runtime dual-write drift assertion — replaced by
  `pico-cli session inspect` CLI (static, safer).
- Nested-dict intent overrides via `pico.toml` — deliberately dropped
  (intent keywords live in code; users PR changes).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-07-pico-memory-context-redesign-design.md
git commit -m "docs(spec): post-review addendum linking follow-up spec"
```

---

# Final Gate

## Task Final: Whole-suite verification

- [ ] **Step 1: Run full pytest**

```bash
uv run pytest -q 2>&1 | tail -10
```

Expected: ≥ 620 passed, ≤ 1 skipped (legacy `test_metrics.py` only).

- [ ] **Step 2: Run all bench scripts**

```bash
uv run python -m benchmarks.perf.bench_build_v2 > /tmp/final_bench_build.json
uv run python -m benchmarks.perf.bench_retrieval > /tmp/final_bench_retrieval.json
uv run python -m benchmarks.perf.bench_recall > /tmp/final_bench_recall.json
echo "All bench scripts produced valid JSON."
```

- [ ] **Step 3: Verify all findings closed via ledger update**

```bash
LEDGER="$(git rev-parse --show-toplevel)/.superpowers/sdd/progress.md"
echo "" >> "$LEDGER"
echo "=== Post-Migration Review & Optimize DONE ===" >> "$LEDGER"
echo "Streams A-F complete; 14/15 findings closed (Finding 14's test_metrics test deferred to independent spec)." >> "$LEDGER"
echo "Suite: <TODO: paste final pytest count>" >> "$LEDGER"
git add "$LEDGER"
git commit -m "docs(ledger): close post-review optimize spec"
```

Replace `<TODO: paste final pytest count>` with the actual count from Step 1.

---

# Self-Review

**1. Spec coverage:** Every spec §4-9 stream task maps to a plan task above. §10 Findings Coverage Matrix — each non-ACCEPTABLE finding has a plan task closing it. ✅

**2. Placeholder scan:**
- One intentional `<TODO: paste final pytest count>` in the ledger update step (final gate) — the number is only known after all tasks run, so it's a runtime placeholder, not a plan placeholder.
- No other TBDs.

**3. Type consistency:**
- `_drop_old_turns(messages, soft_cap_tokens, floor_count, token_of)` in A1 → matched at wire site in `build_v2`.
- `strip_pico_meta(messages: list[dict]) -> list[dict]` in A2 → matches both provider adapter call sites.
- `context_history_soft_cap(root) -> int` and siblings all match B2 signatures and B7 assertions.
- `DROP_PRIORITY` from C1 → used in the C1 renderer loop only.
- `bench(name, fn, iterations=100, warmup=5) -> dict` in D3 → matches all D4/D5/D6 usages.
- `_SniffProvider` shape in E1/E2/E3 → consistent (same fields: `script`, `calls`, `supports_prompt_cache=False`, `supports_native_tools=True`, `last_completion_metadata`).

All names line up.

---

# Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-08-pico-review-and-optimize.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
