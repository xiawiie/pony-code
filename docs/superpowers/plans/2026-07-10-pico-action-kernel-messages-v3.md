# Pico Action Kernel And Messages v3 Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge Pico on one `Response -> decode_action -> Action -> AgentLoop` decision path and one messages-only v3 transcript, while restoring truthful request/completion telemetry, working-summary injection, runtime failure closure, local evidence, and one real native-tool E2E.

**Architecture:** `AgentLoop` owns the top-level turn lifecycle and a frozen injection snapshot; `ContextManager.build_v2` builds only the actual Provider request; `action_codec` is the sole pure decoder; `messages` owns generic canonical-message operations; `SessionStore` validates and atomically migrates v1/v2 to v3. Tool effects remain in the existing executor/recovery stack, with structured status and effect classes instead of string inference.

**Tech Stack:** Python 3.11+, standard library only, pytest, Ruff, uv, existing Anthropic-compatible Provider clients and benchmark harnesses.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-07-10-project-optimization-design.md`.
- Work only on approved C scope. Do not implement A-scope security hardening, a model resolver, Provider registry/gateway, Provider renames, parallel tools, streaming changes, or new dependencies.
- Preserve `Response` and `StopReason`; add only `StopReason.UNKNOWN`.
- Keep one plan because action decoding, request overlay, session cutover, consumer migration, and evidence gates are sequential parts of one runtime vertical slice.
- Use test-driven development: add the focused failing test, run it and confirm the stated failure, implement the smallest change, rerun the focused test, then run the phase gate.
- Do not use tests or benchmark setup through `Pico.record()` or `Pico.record_message()`. Seed valid canonical messages directly with `_pico_meta`.
- Do not read Provider `last_completion_metadata` in `AgentLoop`, report generation, TurnRunner, or live cost accounting. Completion truth comes from each `Response.usage`.
- The completed v3 runtime must not persist injected user text, retry feedback, wire metadata, ignored native calls, thinking blocks, or a `history` mirror. Before Task 15, the only plan-explicit transitional exceptions are the existing v2 `history` mirror (kept only so pre-cutover sessions remain readable, with no migrated consumer depending on it) and Task 8's temporary retry notice, which Task 9 replaces with one-shot request feedback. Do not introduce any other persisted copy.
- Preserve user-owned untracked files. Stage and commit only paths named by the current task.
- Run all commands from the active isolated worktree `/Users/wei/Desktop/pico/.worktrees/action-kernel-messages-v3`.
- Do not run a real API command until every local gate in Task 18 passes.
- For the real gate, run exactly one explicitly selected Provider. Prefer `deepseek` when its canonical key is configured; otherwise use `anthropic`. Never run both as a matrix.

## Completion Contract

The implementation is complete only when all fourteen design criteria are true:

1. `decode_action(Response)` is the only model-decision boundary.
2. Every call's `Response.usage` appears in its `model_turn` and all calls are aggregated in the report.
3. Runtime sessions are schema v3 and transcript state is messages-only.
4. v1/v2 migration is locked, backed up, atomic, failure-safe, and idempotent.
5. retry feedback is visible on exactly the next request.
6. one frozen turn injection remains visible across retries and tool steps.
7. Provider errors and graceful interrupts close TaskState and artifacts without masking the primary exception.
8. persisted tool messages cannot be orphaned; a pair-save failure after a side effect stops as `persistence_error`.
9. tool status/effect metadata is truthful, including `memory_write`.
10. all listed legacy production references are absent.
11. Ruff, pytest, memory-quality, memory ablation, and perf smoke pass.
12. working-file summaries affect the actual v3 request after the bootstrap tool turn has been dropped.
13. one real Anthropic-compatible/DeepSeek run passes and proves a native `ToolAction`.
14. excluded Provider-connection and A-scope work remains unimplemented.

## File Structure

### Create

- `pico/action_codec.py` — action dataclasses plus the pure total decoder.
- `pico/messages.py` — canonical-message construction, request overlay, rendering, metrics, and validation.
- `tests/test_action_codec.py` — exhaustive decoder contract.
- `tests/test_messages.py` — pure message/request-view contract.

### Modify

- `pico/providers/response.py` — add `UNKNOWN` only.
- `pico/providers/fallback_adapter.py` — flatten plus one text-protocol instruction; return raw text `Response`.
- `pico/providers/anthropic_compatible.py` — import shared message stripping and map unknown stop reasons honestly.
- `pico/prompt_prefix.py` — protocol-neutral stable guidance and memory guidance.
- `pico/agent_loop.py` — preflight, snapshot, Action application, copy-on-write commits, usage aggregation, and terminal finalizer.
- `pico/context_manager.py` — actual request assembly only.
- `pico/context/sources.py` — add recent working-file summaries to `memory_index`.
- `pico/runtime.py` — v3 initial shape, report metrics, repeated-tool scan, reset, and legacy API removal.
- `pico/session_store.py` — pure v3 migration plus locked backup/atomic activation.
- `pico/task_state.py` — interrupted/runtime/persistence terminal reasons.
- `pico/tool_executor.py`, `pico/tools.py`, `pico/memory/tools.py`, `pico/repo_map.py` — structured tool outcomes and effect semantics.
- `pico/cli_session.py` — messages-only invariant inspector.
- `pico/evaluation/fixed_benchmark.py`, `pico/evaluation/experiments_synthetic.py`, `pico/evaluation/experiments_real.py`, `pico/evaluation/metrics_reports.py` — messages/request-view consumers.
- `benchmarks/live_e2e/run_live_session.py` and `benchmarks/live_e2e/tests/test_assertions.py` — single-Provider live gate and offline assertions.
- Focused existing tests named in each task.
- `docs/review-pack/README.md`, `docs/review-pack/dashboard.md`, `benchmarks/live_e2e/README.md`, and current evidence documentation.

### Delete after cutover

- `pico/model_output_parser.py`
- `pico/providers/message_utils.py`
- `tests/test_model_output_parser.py`
- `tests/test_provider_message_utils.py`
- The `legacy_string_path` marker from `pyproject.toml`

## Phase Map

| Design phase | Plan tasks | Exit condition |
| --- | --- | --- |
| 0 — green baseline | 1 | Ruff 0 and full pytest green |
| 1 — inert foundations | 2–4 | codec/messages/v3 pure tests green; runtime path unchanged |
| 2 — consumer-first | 5–7 | report/eval/CLI no longer require history |
| 3 — atomic Action switch | 8 | native and fallback both pass only through codec |
| 4 — request-loop convergence | 9–10 | preflight/snapshot/feedback/working summaries use actual request |
| 5 — runtime integrity | 11–13 | COW pairing, effects, error closure |
| 6 — v3 cutover and deletion | 14–15 | new and migrated sessions are v3; structural legacy scan empty |
| 7 — evidence and live harness | 16–17 | offline live tests and current evidence docs pass |
| 8 — final verification | 18–19 | all local gates, then exactly one real E2E |

---

### Task 1: Restore a Green Baseline Before Semantic Work

**Files:**

- Modify: `benchmarks/live_e2e/run_live_session.py`
- Modify: `benchmarks/live_e2e/tests/test_assertions.py`
- Modify: `pico/context/renderer.py`
- Modify: `tests/test_debug_logging.py`
- Modify: `tests/test_memory_block_store_agent_scope.py`
- Modify: `tests/test_memory_retrieval_field_boost.py`
- Modify: `tests/test_memory_retrieval_link.py`
- Modify: `tests/test_runtime_report.py`
- Modify: `tests/test_session_store_migrator.py`

**Interfaces:**

- Consumes: current repository only.
- Produces: no behavior or public-interface changes; exactly the eleven current Ruff findings disappear.

- [ ] **Step 1: Reproduce the lint baseline**

```bash
uv run ruff check .
```

Expected: exit 1 with exactly 11 findings: five E402/F841 findings in live/renderer code and six unused imports in tests.

- [ ] **Step 2: Apply only the mechanical import cleanup**

At the top of `benchmarks/live_e2e/run_live_session.py`, use this import block and remove the later module-level `import time` and `import json`:

```python
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
```

Delete only this unused local from `TurnRunner._extract_first_prompt_and_counts`:

```python
first_prompt: str = ""
```

Move `from pathlib import Path` to the top import block in `benchmarks/live_e2e/tests/test_assertions.py`. In `pico/context/renderer.py`, place the `.sources` import immediately after the `.intent` import and place `logger = logging.getLogger("pico")` after all imports.

Delete only these unused imports:

```text
tests/test_debug_logging.py: import pytest
tests/test_memory_block_store_agent_scope.py: from pathlib import Path
tests/test_memory_retrieval_field_boost.py: from pathlib import Path
tests/test_memory_retrieval_link.py: from pathlib import Path
tests/test_runtime_report.py: import pytest
tests/test_session_store_migrator.py: from pathlib import Path
```

- [ ] **Step 3: Verify Ruff is green**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 4: Establish the full behavioral baseline**

```bash
./scripts/check.sh
```

Expected: Ruff passes; pytest reports 668 passed and 1 legacy skip, with no failures.

- [ ] **Step 5: Commit only the baseline cleanup**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py pico/context/renderer.py tests/test_debug_logging.py tests/test_memory_block_store_agent_scope.py tests/test_memory_retrieval_field_boost.py tests/test_memory_retrieval_link.py tests/test_runtime_report.py tests/test_session_store_migrator.py
git commit -m "chore: restore green quality baseline"
```

---

### Task 2: Add the Pure Action Contract and Total Decoder

**Files:**

- Create: `pico/action_codec.py`
- Create: `tests/test_action_codec.py`
- Modify: `pico/providers/response.py`
- Test: `tests/test_provider_response.py`

**Interfaces:**

- Consumes: `Response(stop_reason: StopReason, content: list[dict], usage: dict)`.
- Produces:

```python
@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict
    tool_use_id: str | None
    origin: Literal["native_tool_use", "text_protocol"]
    ignored_tool_count: int = 0

@dataclass(frozen=True)
class FinalAction:
    text: str
    origin: Literal["provider_text", "text_protocol"]
    truncated: bool = False

@dataclass(frozen=True)
class RetryAction:
    reason_code: str
    notice: str
    origin: Literal["response", "text_protocol"]
    excerpt: str = ""

Action: TypeAlias = ToolAction | FinalAction | RetryAction
```

Decoder signature: `decode_action(response: Response) -> Action`.

- [ ] **Step 1: Write the failing decoder tests**

Create `tests/test_action_codec.py`:

```python
import pytest

from pico.action_codec import FinalAction, RetryAction, ToolAction, decode_action
from pico.providers.response import Response, StopReason


def response(*content, stop=StopReason.END_TURN):
    return Response(stop_reason=stop, content=list(content), usage={})


def text(value):
    return {"type": "text", "text": value}


def tool(name="read_file", arguments=None, tool_use_id="toolu_1"):
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": {"path": "README.md"} if arguments is None else arguments,
    }


def test_native_tool_has_highest_priority_and_counts_ignored_calls():
    action = decode_action(
        response(
            text("<final>do not finish</final>"),
            tool(),
            tool(name="search", arguments={"pattern": "x"}, tool_use_id="toolu_2"),
            stop=StopReason.STOP_SEQUENCE,
        )
    )
    assert action == ToolAction(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_1",
        origin="native_tool_use",
        ignored_tool_count=1,
    )


@pytest.mark.parametrize(
    "bad_block",
    [
        tool(name=""),
        tool(arguments=["README.md"]),
        {"type": "tool_use", "id": "x", "input": {}},
    ],
)
def test_first_invalid_native_tool_retries_without_skipping_second(bad_block):
    action = decode_action(response(bad_block, tool(name="search", arguments={"pattern": "x"})))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "invalid_native_tool"
    assert action.origin == "response"


@pytest.mark.parametrize(
    ("raw", "name", "arguments"),
    [
        ('<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "read_file", {"path": "README.md"}),
        ('<tool>{"name":"search","arguments":{"pattern":"x"}}</tool>', "search", {"pattern": "x"}),
        ('<tool name="write_file" path="a.py"><content>print("ok")</content></tool>', "write_file", {"path": "a.py", "content": 'print("ok")'}),
        ('<tool name="list_files" path="." />', "list_files", {"path": "."}),
    ],
)
def test_leading_text_tool_protocol(raw, name, arguments):
    action = decode_action(response(text("  " + raw)))
    assert action == ToolAction(
        name=name,
        arguments=arguments,
        tool_use_id=None,
        origin="text_protocol",
    )


@pytest.mark.parametrize(
    "raw",
    [
        'Example: <tool>{"name":"read_file","args":{}}</tool>',
        '\x60\x60\x60xml\n<tool>{"name":"read_file","args":{}}</tool>\n\x60\x60\x60',
        '> <tool>{"name":"read_file","args":{}}</tool>',
        '"<tool>{\\"name\\":\\"read_file\\",\\"args\\":{}}</tool>"',
        "<toolbox>not a call</toolbox>",
    ],
)
def test_nonleading_or_similar_tool_tags_are_provider_text(raw):
    assert decode_action(response(text(raw))) == FinalAction(
        text=raw,
        origin="provider_text",
    )


def test_all_nonempty_text_blocks_are_joined_in_order():
    assert decode_action(response(text("alpha"), text(""), text("beta"))) == FinalAction(
        text="alpha\nbeta",
        origin="provider_text",
    )


def test_leading_final_protocol_unwraps_nonempty_body():
    assert decode_action(response(text(" <final>Done.</final>"))) == FinalAction(
        text="Done.",
        origin="text_protocol",
    )


def test_max_tokens_preserves_incomplete_final_body_as_truncated():
    assert decode_action(
        response(text("<final>Partial answer"), stop=StopReason.MAX_TOKENS)
    ) == FinalAction(
        text="Partial answer",
        origin="text_protocol",
        truncated=True,
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("<tool>{bad json}</tool>", "malformed_tool_protocol"),
        ("<final></final>", "empty_final_protocol"),
        ("", "empty_response"),
    ],
)
def test_protocol_and_empty_failures_are_bounded_retries(raw, reason):
    action = decode_action(response(text(raw)) if raw else response())
    assert isinstance(action, RetryAction)
    assert action.reason_code == reason
    assert len(action.excerpt) <= 160
    if raw:
        assert raw not in action.notice


def test_stop_sequence_with_text_is_not_a_final_answer():
    action = decode_action(response(text("not complete"), stop=StopReason.STOP_SEQUENCE))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "stop_sequence"


def test_unknown_stop_reason_is_not_treated_as_end_turn():
    action = decode_action(response(text("ambiguous"), stop=StopReason.UNKNOWN))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "unsupported_response_shape"


def test_max_tokens_plain_text_is_marked_truncated():
    assert decode_action(
        response(text("partial but useful"), stop=StopReason.MAX_TOKENS)
    ) == FinalAction(
        text="partial but useful",
        origin="provider_text",
        truncated=True,
    )


def test_unsupported_content_shape_is_a_total_retry():
    action = decode_action(response({"type": "image", "source": "x"}))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "unsupported_response_shape"
```

- [ ] **Step 2: Confirm the tests fail for the missing module**

```bash
uv run pytest tests/test_action_codec.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'pico.action_codec'`.

- [ ] **Step 3: Add `StopReason.UNKNOWN`**

Update `pico/providers/response.py`:

```python
class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    UNKNOWN = "unknown"
```

Add `assert StopReason.UNKNOWN == "unknown"` to `tests/test_provider_response.py::test_stop_reason_enum_values`.

- [ ] **Step 4: Add the dataclasses, bounded notices, and native decoding**

Create `pico/action_codec.py` with this first half:

```python
"""Pure decoding from provider Responses to runtime Actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .providers.response import Response, StopReason

_EXCERPT_LIMIT = 160
_ATTR_RE = re.compile(
    r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_ATTRIBUTE_TOOL_OPEN_RE = re.compile(r"^<tool(?=\s|/?>)")


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict
    tool_use_id: str | None
    origin: Literal["native_tool_use", "text_protocol"]
    ignored_tool_count: int = 0


@dataclass(frozen=True)
class FinalAction:
    text: str
    origin: Literal["provider_text", "text_protocol"]
    truncated: bool = False


@dataclass(frozen=True)
class RetryAction:
    reason_code: str
    notice: str
    origin: Literal["response", "text_protocol"]
    excerpt: str = ""


Action: TypeAlias = ToolAction | FinalAction | RetryAction

_NOTICES = {
    "empty_response": "Runtime notice: the model returned no actionable content. Return one tool call or a non-empty final answer.",
    "malformed_tool_protocol": "Runtime notice: the text tool call was malformed. Return one valid tool call or a non-empty final answer.",
    "empty_final_protocol": "Runtime notice: the final answer was empty or incomplete. Return a non-empty final answer.",
    "invalid_native_tool": "Runtime notice: the native tool call had an invalid name or arguments object. Return one valid tool call.",
    "stop_sequence": "Runtime notice: the model stopped before completing an action. Return one tool call or a non-empty final answer.",
    "unsupported_response_shape": "Runtime notice: the model response shape was unsupported. Return one tool call or a non-empty final answer.",
}


def _retry(reason_code, origin, raw=""):
    return RetryAction(
        reason_code=reason_code,
        notice=_NOTICES[reason_code],
        origin=origin,
        excerpt=str(raw).strip()[:_EXCERPT_LIMIT],
    )


def _joined_text(content):
    parts = []
    saw_unsupported = False
    for block in content:
        if not isinstance(block, dict):
            saw_unsupported = True
            continue
        if block.get("type") == "text":
            value = str(block.get("text", "") or "")
            if value.strip():
                parts.append(value.strip())
        elif block.get("type") != "tool_use":
            saw_unsupported = True
    return "\n".join(parts), saw_unsupported


def _native_action(tool_blocks):
    first = tool_blocks[0]
    name = first.get("name")
    arguments = first.get("input")
    tool_use_id = first.get("id")
    if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
        return _retry("invalid_native_tool", "response", first)
    if tool_use_id is not None and not isinstance(tool_use_id, str):
        return _retry("invalid_native_tool", "response", first)
    return ToolAction(
        name=name.strip(),
        arguments=dict(arguments),
        tool_use_id=tool_use_id or None,
        origin="native_tool_use",
        ignored_tool_count=len(tool_blocks) - 1,
    )
```

- [ ] **Step 5: Complete strict text-protocol parsing and the total decision order**

Append this second half to `pico/action_codec.py`:

```python
def _tag_body(text, tag):
    opening = f"<{tag}>"
    closing = f"</{tag}>"
    body_start = len(opening)
    body_end = text.find(closing, body_start)
    if body_end < 0:
        return text[body_start:], False
    return text[body_start:body_end], True


def _attrs(text):
    values = {}
    for match in _ATTR_RE.finditer(text):
        values[match.group(1)] = (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
    return values


def _nested_value(body, key):
    opening = f"<{key}>"
    closing = f"</{key}>"
    start = body.find(opening)
    if start < 0:
        return None
    start += len(opening)
    end = body.find(closing, start)
    return body[start:] if end < 0 else body[start:end]


def _attribute_tool(text):
    open_end = text.find(">")
    if open_end < 0:
        return None
    self_closing = text[:open_end].rstrip().endswith("/")
    close_start = text.find("</tool>", open_end + 1)
    if not self_closing and close_start < 0:
        return None
    values = _attrs(text[len("<tool"):open_end])
    name = str(values.pop("name", "")).strip()
    if not name:
        return None
    body = "" if self_closing else text[open_end + 1:close_start]
    arguments = dict(values)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        nested = _nested_value(body, key)
        if nested is not None:
            arguments[key] = nested
    if name == "write_file" and "content" not in arguments and body.strip():
        arguments["content"] = body.strip("\n")
    if name == "delegate" and "task" not in arguments and body.strip():
        arguments["task"] = body.strip()
    return ToolAction(
        name=name,
        arguments=arguments,
        tool_use_id=None,
        origin="text_protocol",
    )


def _text_tool(text):
    if text.startswith("<tool>"):
        body, closed = _tag_body(text, "tool")
        if not closed:
            return _retry("malformed_tool_protocol", "text_protocol", text)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        if not isinstance(payload, dict):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        name = payload.get("name")
        arguments = payload.get("args", payload.get("arguments", {}))
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        return ToolAction(
            name=name.strip(),
            arguments=dict(arguments),
            tool_use_id=None,
            origin="text_protocol",
        )
    action = _attribute_tool(text)
    return action or _retry("malformed_tool_protocol", "text_protocol", text)


def decode_action(response: Response) -> Action:
    content = list(response.content or [])
    tool_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if tool_blocks:
        return _native_action(tool_blocks)

    merged_text, saw_unsupported = _joined_text(content)
    leading = merged_text.lstrip()

    if leading.startswith("<tool>") or _ATTRIBUTE_TOOL_OPEN_RE.match(leading):
        return _text_tool(leading)

    if leading.startswith("<final>"):
        body, closed = _tag_body(leading, "final")
        body = body.strip()
        if body and (closed or response.stop_reason == StopReason.MAX_TOKENS):
            return FinalAction(
                text=body,
                origin="text_protocol",
                truncated=not closed or response.stop_reason == StopReason.MAX_TOKENS,
            )
        return _retry("empty_final_protocol", "text_protocol", leading)

    if response.stop_reason == StopReason.END_TURN and merged_text:
        return FinalAction(text=merged_text, origin="provider_text")
    if response.stop_reason == StopReason.MAX_TOKENS and merged_text:
        return FinalAction(
            text=merged_text,
            origin="provider_text",
            truncated=True,
        )
    if response.stop_reason == StopReason.STOP_SEQUENCE:
        return _retry("stop_sequence", "response", merged_text)
    if not merged_text and not saw_unsupported:
        return _retry("empty_response", "response")
    return _retry("unsupported_response_shape", "response", merged_text or content)
```

- [ ] **Step 6: Run the focused decoder and Response tests**

```bash
uv run pytest tests/test_action_codec.py tests/test_provider_response.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Run the inert-foundation regression slice**

```bash
uv run pytest tests/test_model_output_parser.py tests/test_provider_fallback.py tests/test_agent_loop.py -q
```

Expected: all existing tests pass; the new decoder is not wired into runtime yet.

- [ ] **Step 8: Commit the inert Action foundation**

```bash
git add pico/action_codec.py pico/providers/response.py tests/test_action_codec.py tests/test_provider_response.py
git commit -m "feat(action-kernel): define pure response decoder"
```

---

### Task 3: Add Pure Canonical-Message Operations

**Files:**

- Create: `pico/messages.py`
- Create: `tests/test_messages.py`

**Interfaces:**

- `MessageValidationError` subclasses `ValueError`.
- `append_messages(messages, *new_messages) -> list[dict]`.
- `replace_latest_plain_user(messages, rendered_user) -> list[dict]`.
- `build_request_messages(messages, *, rendered_user, runtime_feedback="") -> list[dict]`.
- `strip_pico_meta(messages) -> list[dict]`.
- `make_tool_pair` consumes name, arguments, id, result, timestamp, status, effect, optional change id/meta and returns two message dicts.
- `message_content_text(message) -> str`.
- `render_transcript(messages) -> str`.
- `message_metrics(messages, token_of) -> dict`.
- `validate_messages(messages, *, require_meta) -> None`.

- [ ] **Step 1: Write failing request-overlay and construction tests**

Create `tests/test_messages.py`:

```python
import copy

import pytest

from pico.messages import (
    MessageValidationError,
    append_messages,
    build_request_messages,
    make_tool_pair,
    message_metrics,
    render_transcript,
    strip_pico_meta,
    validate_messages,
)


def plain(role, content, created_at="2026-07-10T00:00:00Z"):
    return {"role": role, "content": content, "_pico_meta": {"created_at": created_at}}


def test_request_overlay_replaces_latest_plain_user_and_ignores_tool_result_carrier():
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_1",
        result_content="body",
        created_at="2026-07-10T00:00:01Z",
        tool_status="ok",
        effect_class="read_only",
    )
    source = [plain("user", "question"), *pair]
    before = copy.deepcopy(source)
    request = build_request_messages(
        source,
        rendered_user="<system-reminder>snapshot</system-reminder>\nquestion",
        runtime_feedback="use a valid tool call",
    )
    assert source == before
    assert "snapshot" in request[0]["content"]
    assert "<pico:runtime_feedback>" in request[0]["content"]
    assert request[-1]["content"][0]["type"] == "tool_result"
    assert all("_pico_meta" not in message for message in request)


def test_runtime_feedback_is_absent_when_empty():
    request = build_request_messages(
        [plain("user", "question")],
        rendered_user="snapshot\nquestion",
    )
    assert "runtime_feedback" not in request[0]["content"]


def test_append_messages_does_not_mutate_input():
    source = [plain("user", "q")]
    result = append_messages(source, plain("assistant", "a"))
    assert len(source) == 1
    assert [item["role"] for item in result] == ["user", "assistant"]


def test_tool_pair_has_matching_id_error_semantics_and_metadata():
    assistant, result = make_tool_pair(
        name="run_shell",
        arguments={"command": "false"},
        tool_use_id="toolu_2",
        result_content="exit_code: 1",
        created_at="2026-07-10T00:00:00Z",
        tool_status="error",
        effect_class="workspace_write",
        tool_change_id="tc_1",
    )
    assert assistant["content"][0]["id"] == "toolu_2"
    assert result["content"][0]["tool_use_id"] == "toolu_2"
    assert result["content"][0]["is_error"] is True
    assert result["_pico_meta"]["tool_status"] == "error"
    assert result["_pico_meta"]["effect_class"] == "workspace_write"
    assert result["_pico_meta"]["tool_change_id"] == "tc_1"


def test_strip_pico_meta_returns_new_top_level_dicts():
    source = [plain("user", "q")]
    cleaned = strip_pico_meta(source)
    assert "_pico_meta" not in cleaned[0]
    assert "_pico_meta" in source[0]
    assert cleaned[0] is not source[0]


def test_render_and_metrics_use_content_not_internal_meta():
    messages = [plain("user", "question"), plain("assistant", "answer")]
    rendered = render_transcript(messages)
    metrics = message_metrics(messages, token_of=lambda value: len(value))
    assert "[user] question" in rendered
    assert "[assistant] answer" in rendered
    assert "created_at" not in rendered
    assert metrics == {
        "messages_count": 2,
        "messages_chars": len("question") + len("answer"),
        "messages_tokens": len("question") + len("answer"),
    }


def test_validate_messages_accepts_a_complete_pair():
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_ok",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
    )
    validate_messages(
        [plain("user", "q"), *pair, plain("assistant", "done")],
        require_meta=True,
    )


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "system", "content": "bad", "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "bad"}], "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "", "name": "x", "input": {}}], "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "x", "input": {}}], "_pico_meta": {}}],
    ],
)
def test_validate_messages_rejects_bad_roles_blocks_ids_and_orphans(messages):
    with pytest.raises(MessageValidationError):
        validate_messages(messages, require_meta=True)
```

- [ ] **Step 2: Confirm the missing-module failure**

```bash
uv run pytest tests/test_messages.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'pico.messages'`.

- [ ] **Step 3: Implement construction, overlay, rendering, and metrics**

Create the first part of `pico/messages.py`:

```python
"""Pure operations for Pico's canonical message transcript."""

from __future__ import annotations

import json


class MessageValidationError(ValueError):
    """A canonical transcript violates the v3 message contract."""


def append_messages(messages, *new_messages):
    return [*list(messages or []), *new_messages]


def replace_latest_plain_user(messages, rendered_user):
    copied = [dict(message) for message in list(messages or [])]
    for index in range(len(copied) - 1, -1, -1):
        message = copied[index]
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = str(rendered_user)
            return copied
    raise MessageValidationError("request view has no top-level plain user message")


def strip_pico_meta(messages):
    cleaned = []
    for message in list(messages or []):
        item = dict(message)
        item.pop("_pico_meta", None)
        cleaned.append(item)
    return cleaned


def build_request_messages(messages, *, rendered_user, runtime_feedback=""):
    content = str(rendered_user)
    feedback = str(runtime_feedback or "").strip()
    if feedback:
        content += (
            "\n\n<system-reminder>\n"
            "<pico:runtime_feedback>\n"
            + feedback
            + "\n</pico:runtime_feedback>\n"
            "</system-reminder>"
        )
    return strip_pico_meta(replace_latest_plain_user(messages, content))


def make_tool_pair(
    *,
    name,
    arguments,
    tool_use_id,
    result_content,
    created_at,
    tool_status,
    effect_class,
    tool_change_id="",
    result_meta=None,
):
    result_meta = dict(result_meta or {})
    assistant = {
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": str(tool_use_id),
            "name": str(name),
            "input": dict(arguments),
        }],
        "_pico_meta": {
            "created_at": str(created_at),
            "tool_use_id": str(tool_use_id),
        },
    }
    result_block = {
        "type": "tool_result",
        "tool_use_id": str(tool_use_id),
        "content": str(result_content),
    }
    if tool_status in {"rejected", "error", "partial_success"}:
        result_block["is_error"] = True
    metadata = {
        "created_at": str(created_at),
        "tool_use_id": str(tool_use_id),
        "tool_status": str(tool_status),
        "effect_class": str(effect_class),
        **result_meta,
    }
    if tool_change_id:
        metadata["tool_change_id"] = str(tool_change_id)
    return assistant, {
        "role": "user",
        "content": [result_block],
        "_pico_meta": metadata,
    }


def message_content_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            parts.append(str(block.get("name", "")))
            parts.append(json.dumps(block.get("input", {}), sort_keys=True))
        elif block.get("type") == "tool_result":
            parts.append(str(block.get("content", "")))
    return "\n".join(parts)


def render_transcript(messages):
    lines = []
    for message in list(messages or []):
        role = str(message.get("role", ""))
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                lines.append(
                    f"[assistant:tool_use id={block.get('id', '')}] "
                    f"{block.get('name', '')}("
                    f"{json.dumps(block.get('input', {}), sort_keys=True)})"
                )
            elif block.get("type") == "tool_result":
                lines.append(
                    f"[user:tool_result id={block.get('tool_use_id', '')}] "
                    f"{block.get('content', '')}"
                )
            elif block.get("type") == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
    return "\n".join(lines)


def message_metrics(messages, token_of):
    values = [message_content_text(message) for message in list(messages or [])]
    return {
        "messages_count": len(values),
        "messages_chars": sum(len(value) for value in values),
        "messages_tokens": sum(int(token_of(value)) for value in values),
    }
```

- [ ] **Step 4: Implement strict v3 message validation**

Append to `pico/messages.py`:

```python
def _tool_block(message, expected_type, expected_role):
    if message.get("role") != expected_role:
        raise MessageValidationError(
            f"{expected_type} must use role={expected_role}"
        )
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise MessageValidationError(
            f"{expected_type} message must contain exactly one block"
        )
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != expected_type:
        raise MessageValidationError(f"invalid {expected_type} block")
    return block


def validate_messages(messages, *, require_meta):
    if not isinstance(messages, list):
        raise MessageValidationError("messages must be a list")
    seen_ids = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            raise MessageValidationError("message must be an object")
        if message.get("role") not in {"user", "assistant"}:
            raise MessageValidationError("message role must be user or assistant")
        if require_meta and not isinstance(message.get("_pico_meta"), dict):
            raise MessageValidationError("_pico_meta must be an object")
        if not require_meta and "_pico_meta" in message and not isinstance(
            message.get("_pico_meta"), dict
        ):
            raise MessageValidationError("_pico_meta must be an object")
        content = message.get("content")
        if isinstance(content, str):
            index += 1
            continue
        if not isinstance(content, list) or not content:
            raise MessageValidationError("message content must be a string or blocks")
        first_type = content[0].get("type") if isinstance(content[0], dict) else ""
        if first_type == "tool_use":
            block = _tool_block(message, "tool_use", "assistant")
            tool_use_id = block.get("id")
            if (
                not isinstance(tool_use_id, str)
                or not tool_use_id
                or tool_use_id in seen_ids
                or not isinstance(block.get("name"), str)
                or not block.get("name")
                or not isinstance(block.get("input"), dict)
            ):
                raise MessageValidationError("invalid or duplicate tool_use")
            seen_ids.add(tool_use_id)
            if index + 1 >= len(messages):
                raise MessageValidationError("orphan tool_use")
            result = _tool_block(messages[index + 1], "tool_result", "user")
            if result.get("tool_use_id") != tool_use_id:
                raise MessageValidationError("tool_result id does not match")
            index += 2
            continue
        if first_type == "tool_result":
            raise MessageValidationError("orphan tool_result")
        if any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in content
        ):
            raise MessageValidationError("unsupported content block")
        index += 1
```

- [ ] **Step 5: Run the pure message tests**

```bash
uv run pytest tests/test_messages.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the inert message foundation**

```bash
git add pico/messages.py tests/test_messages.py
git commit -m "feat(messages): add canonical message primitives"
```

---

### Task 4: Specify Pure Session v3 Migration Before Activating I/O

**Files:**

- Modify: `pico/session_store.py`
- Modify: `pico/messages.py`
- Modify: `tests/test_session_store_migrator.py`
- Modify: `tests/test_messages.py`

**Interfaces:**

- `SessionMigrationError` subclasses `ValueError`.
- `migrate_session_to_v3(session: dict) -> dict`.

The function is pure in this task: it deep-copies input, validates transcript candidates, preserves non-transcript keys, removes `history`, and returns schema v3. `SessionStore.load` is not switched until Task 14.

- [ ] **Step 1: Add pure v3 migration tests**

Append to `tests/test_session_store_migrator.py`:

```python
import copy

from pico.messages import validate_messages
from pico.session_store import SessionMigrationError, migrate_session_to_v3


def _valid_v2_messages():
    return [
        {"role": "user", "content": "q", "_pico_meta": {"created_at": "t1"}},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "_pico_meta": {"created_at": "t2"},
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "body"}],
            "_pico_meta": {"created_at": "t2"},
        },
        {"role": "assistant", "content": "done", "_pico_meta": {"created_at": "t3"}},
    ]


def test_v2_prefers_valid_nonempty_messages_and_preserves_nontranscript_state():
    source = {
        "id": "s2",
        "schema_version": 2,
        "messages": _valid_v2_messages(),
        "history": [{"role": "user", "content": "stale mirror"}],
        "working_memory": {"task_summary": "goal", "recent_files": ["a.py"]},
        "memory": {"file_summaries": {"a.py": {"summary": "fact"}}},
        "recently_recalled": ["note"],
        "checkpoints": {"current_id": "c1", "items": {"c1": {}}},
        "runtime_identity": {"workspace_fingerprint": "fp"},
        "resume_state": {"status": "full-valid"},
        "recovery": {"current_checkpoint_id": "r1"},
    }
    before = copy.deepcopy(source)
    migrated = migrate_session_to_v3(source)
    assert source == before
    assert migrated["schema_version"] == 3
    assert "history" not in migrated
    assert migrated["messages"] == before["messages"]
    for key in (
        "working_memory",
        "memory",
        "recently_recalled",
        "checkpoints",
        "runtime_identity",
        "resume_state",
        "recovery",
    ):
        assert migrated[key] == before[key]
    validate_messages(migrated["messages"], require_meta=True)


def test_v2_empty_messages_rebuilds_from_nonempty_history():
    migrated = migrate_session_to_v3({
        "id": "s2",
        "schema_version": 2,
        "messages": [],
        "history": [
            {"role": "user", "content": "q", "created_at": "t1"},
            {"role": "tool", "name": "read_file", "args": {"path": "a.py"}, "content": "body", "created_at": "t2"},
            {"role": "assistant", "content": "done", "created_at": "t3"},
        ],
    })
    assert [message["role"] for message in migrated["messages"]] == [
        "user", "assistant", "user", "assistant"
    ]
    validate_messages(migrated["messages"], require_meta=True)


def test_v2_invalid_messages_recovers_from_valid_history():
    migrated = migrate_session_to_v3({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "orphan", "name": "x", "input": {}}]}],
        "history": [{"role": "user", "content": "recover me", "created_at": "t"}],
    })
    assert migrated["messages"][0]["content"] == "recover me"


def test_unknown_history_role_fails_without_mutating_input():
    source = {
        "id": "bad",
        "schema_version": 1,
        "history": [{"role": "runtime", "content": "do not skip"}],
    }
    before = copy.deepcopy(source)
    with pytest.raises(SessionMigrationError, match="unknown history role"):
        migrate_session_to_v3(source)
    assert source == before


def test_empty_v1_history_migrates_to_empty_v3_messages():
    migrated = migrate_session_to_v3({
        "id": "empty",
        "schema_version": 1,
        "history": [],
    })
    assert migrated["schema_version"] == 3
    assert migrated["messages"] == []
    assert "history" not in migrated


def test_v1_history_is_authoritative_over_stray_messages():
    migrated = migrate_session_to_v3({
        "id": "v1",
        "schema_version": 1,
        "messages": [{
            "role": "user",
            "content": "stray",
            "_pico_meta": {},
        }],
        "history": [{
            "role": "user",
            "content": "authoritative",
            "created_at": "t",
        }],
    })
    assert migrated["messages"][0]["content"] == "authoritative"


@pytest.mark.parametrize("schema_version", [None, True, "", 1.5, float("inf")])
def test_invalid_schema_versions_raise_session_migration_error(schema_version):
    with pytest.raises(SessionMigrationError, match="session schema version"):
        migrate_session_to_v3({
            "id": "bad-version",
            "schema_version": schema_version,
            "history": [],
        })


def test_unhashable_history_role_raises_session_migration_error():
    with pytest.raises(SessionMigrationError, match="unknown history role"):
        migrate_session_to_v3({
            "id": "bad-role",
            "schema_version": 1,
            "history": [{"role": [], "content": "x"}],
        })


def test_v3_is_validated_and_returned_without_history():
    source = {"id": "s3", "schema_version": 3, "messages": _valid_v2_messages()}
    assert migrate_session_to_v3(source) == source


def test_v3_with_orphan_is_rejected():
    with pytest.raises(SessionMigrationError, match="orphan"):
        migrate_session_to_v3({
            "id": "s3",
            "schema_version": 3,
            "messages": [{
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "x", "name": "read_file", "input": {}}],
                "_pico_meta": {},
            }],
        })
```

Append to `tests/test_messages.py`:

```python
def test_validate_messages_rejects_unhashable_role():
    with pytest.raises(MessageValidationError, match="role"):
        validate_messages(
            [{"role": [], "content": "bad", "_pico_meta": {}}],
            require_meta=True,
        )
```

- [ ] **Step 2: Confirm the new API is absent**

```bash
uv run pytest tests/test_session_store_migrator.py -q
```

Expected: collection fails because `SessionMigrationError` and `migrate_session_to_v3` do not exist.

- [ ] **Step 3: Add strict history conversion**

In `pico/session_store.py`, retain current I/O temporarily, import `deepcopy` and the message validator, then add:

```python
class SessionMigrationError(ValueError):
    """A legacy session cannot be converted without losing transcript data."""


def _history_to_messages(history):
    if not isinstance(history, list):
        raise SessionMigrationError("history must be a list")
    messages = []
    for entry in history:
        if not isinstance(entry, dict):
            raise SessionMigrationError("history entry must be an object")
        role = entry.get("role")
        created_at = entry.get("created_at")
        if not isinstance(role, str):
            raise SessionMigrationError(f"unknown history role: {role!r}")
        if role in {"user", "assistant"}:
            content = entry.get("content")
            if not isinstance(content, str):
                raise SessionMigrationError("plain history content must be text")
            messages.append({
                "role": role,
                "content": content,
                "_pico_meta": {"created_at": created_at} if created_at else {},
            })
            continue
        if role == "tool":
            name = entry.get("name")
            arguments = entry.get("args", {})
            content = entry.get("content")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
                or not isinstance(content, str)
            ):
                raise SessionMigrationError("invalid tool history entry")
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            meta = {"tool_use_id": tool_use_id}
            if created_at:
                meta["created_at"] = created_at
            messages.extend([
                {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": name,
                        "input": dict(arguments),
                    }],
                    "_pico_meta": dict(meta),
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }],
                    "_pico_meta": dict(meta),
                },
            ])
            continue
        raise SessionMigrationError(f"unknown history role: {role!r}")
    return messages
```

In `pico/messages.py`, make the shared validator reject an unhashable role
with its documented error type before set membership:

```python
        role = message.get("role")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            raise MessageValidationError("message role must be user or assistant")
```

- [ ] **Step 4: Add the pure v3 selector**

Append:

```python
def _normalized_messages(messages):
    normalized = deepcopy(messages)
    if isinstance(normalized, list):
        for message in normalized:
            if isinstance(message, dict):
                message.setdefault("_pico_meta", {})
    validate_messages(normalized, require_meta=True)
    return normalized


def migrate_session_to_v3(session):
    if not isinstance(session, dict):
        raise SessionMigrationError("session must be an object")
    migrated = deepcopy(session)
    raw_version = migrated.get("schema_version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, (int, float, str))
        or (isinstance(raw_version, float) and not raw_version.is_integer())
    ):
        raise SessionMigrationError("invalid session schema version")
    try:
        version = int(raw_version)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SessionMigrationError("invalid session schema version") from exc
    if version not in {1, 2, 3}:
        raise SessionMigrationError(
            f"unsupported session schema version: {version}"
        )
    history = migrated.get("history", [])

    if version == 3:
        if "history" in migrated:
            raise SessionMigrationError("v3 session must not contain history")
        try:
            validate_messages(migrated.get("messages"), require_meta=True)
        except MessageValidationError as exc:
            raise SessionMigrationError(str(exc)) from exc
        return migrated

    if version == 1:
        selected = _history_to_messages(history)
    else:
        messages = migrated.get("messages")
        selected = None
        if isinstance(messages, list) and messages:
            try:
                selected = _normalized_messages(messages)
            except MessageValidationError:
                selected = None
        if selected is None:
            if isinstance(history, list) and history:
                selected = _history_to_messages(history)
            elif isinstance(messages, list) and not messages:
                selected = []
            else:
                raise SessionMigrationError("session has no valid transcript")
    try:
        validate_messages(selected, require_meta=True)
    except MessageValidationError as exc:
        raise SessionMigrationError(str(exc)) from exc

    migrated["messages"] = selected
    migrated.pop("history", None)
    migrated["schema_version"] = 3
    return migrated
```

- [ ] **Step 5: Run pure migration tests and existing SessionStore tests**

```bash
uv run pytest tests/test_messages.py tests/test_session_store_migrator.py tests/test_session_store.py -q
```

Expected: all pass. Existing load/save remains on its old I/O path in this task.

- [ ] **Step 6: Run the Phase 1 gate**

```bash
./scripts/check.sh
```

Expected: Ruff and full pytest pass; the single legacy memory-ablation skip remains.

- [ ] **Step 7: Commit the pure v3 contract**

```bash
git add pico/session_store.py pico/messages.py tests/test_session_store_migrator.py tests/test_messages.py
git commit -m "feat(session): specify messages v3 migration"
```

---

### Task 5: Migrate Runtime Reports and Metrics to Canonical Messages

**Files:**

- Modify: `pico/messages.py`
- Modify: `pico/runtime.py`
- Modify: `pico/evaluation/metrics_reports.py`
- Modify: `pico/evaluation/provider_benchmark.py`
- Modify: `pico/evaluation/experiments_recovery.py`
- Modify: `benchmarks/coding_tasks.json`
- Modify: `tests/test_runtime_report.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_metadata_completeness.py`
- Modify: `tests/test_run_store.py`
- Modify: `tests/test_messages.py`

**Interfaces:**

`Pico.build_report(task_state, *, completion_usage_totals=None) -> dict`.

The report keys become `last_request_metadata`, `completion_usage_totals`, `session_messages_count`, `session_messages_chars`, `session_messages_tokens`, `session_tool_event_count`, `session_tool_name_counts`, and `session_tool_status_counts`. The old `prompt_metadata` report key is removed in this task.

- [ ] **Step 1: Add report truth-source tests**

Add `from pico.task_state import TaskState` to the test imports, then add to `tests/test_runtime_report.py`:

```python
def test_report_separates_sent_request_session_transcript_and_all_completion_usage(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.session["messages"] = [
        {"role": "user", "content": "older question", "_pico_meta": {"created_at": "t1"}},
        {"role": "assistant", "content": "older answer", "_pico_meta": {"created_at": "t2"}},
    ]
    agent.last_prompt_metadata = {
        "messages_count": 1,
        "messages_chars": 8,
        "messages_tokens": 2,
        "system_cache_key": "cache",
    }
    task_state = TaskState.create(task_id="task_x", run_id="run_x", user_request="q")
    task_state.finish_success("done")
    report = agent.build_report(
        task_state,
        completion_usage_totals={
            "input_tokens": 30,
            "output_tokens": 7,
            "total_tokens": 37,
            "cached_tokens": 10,
            "cache_creation_input_tokens": 4,
            "cache_read_input_tokens": 10,
            "cache_hit": True,
        },
    )
    assert report["last_request_metadata"]["messages_count"] == 1
    assert report["session_messages_count"] == 2
    assert report["session_messages_chars"] == len("older questionolder answer")
    assert report["completion_usage_totals"]["total_tokens"] == 37
    assert report["completion_usage_totals"]["cache_hit"] is True
    assert "prompt_metadata" not in report
    assert "older question" not in json.dumps(report)
    assert "older answer" not in json.dumps(report)
```

Update report/metrics assertions so cache usage is read from `completion_usage_totals`, prefix reuse and dropped-message fields are read from `last_request_metadata`, and full transcript size is read from `session_messages_*`.

- [ ] **Step 2: Run the focused report tests and confirm failure**

```bash
uv run pytest tests/test_runtime_report.py tests/test_metrics.py tests/test_metadata_completeness.py -q
```

Expected: failures for missing report keys and the still-present `prompt_metadata` key.

- [ ] **Step 3: Replace the report metadata helper and report body**

In `pico/messages.py` add:

```python
def tool_event_metrics(messages):
    name_counts = {}
    status_counts = {}
    event_count = 0
    for message in list(messages or []):
        content = message.get("content")
        if (
            message.get("role") == "assistant"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_use"
        ):
            name = str(content[0].get("name", "") or "")
            event_count += 1
            if name:
                name_counts[name] = name_counts.get(name, 0) + 1
        if (
            message.get("role") == "user"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_result"
        ):
            metadata = message.get("_pico_meta", {})
            status = (
                str(metadata.get("tool_status", "") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "event_count": event_count,
        "name_counts": name_counts,
        "status_counts": status_counts,
    }
```

Add to `tests/test_messages.py`:

```python
def test_tool_event_metrics_counts_names_and_result_statuses():
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="tu_metrics",
        result_content="body",
        created_at="t",
        tool_status="ok",
        effect_class="read_only",
    )
    assert tool_event_metrics(pair) == {
        "event_count": 1,
        "name_counts": {"read_file": 1},
        "status_counts": {"ok": 1},
    }
```

Import `tool_event_metrics` in that test module. In `pico/runtime.py`, rename `build_report_checkpoint_metadata` to `build_report_request_metadata` without changing its resume-status preservation behavior. Import `message_metrics` and `tool_event_metrics`, then replace `Pico.build_report` with:

```python
def build_report(self, task_state, *, completion_usage_totals=None):
    request_metadata = build_report_request_metadata(
        task_state,
        self.last_prompt_metadata,
    )
    session_metrics = message_metrics(
        self.session.get("messages", []),
        token_of=self.context_manager._count_tokens_for_v2,
    )
    tool_metrics = tool_event_metrics(self.session.get("messages", []))
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_hit": False,
        **dict(completion_usage_totals or {}),
    }
    return {
        "run_id": task_state.run_id,
        "task_id": task_state.task_id,
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
        "final_answer": task_state.final_answer,
        "tool_steps": task_state.tool_steps,
        "attempts": task_state.attempts,
        "checkpoint_id": task_state.checkpoint_id,
        "resume_status": task_state.resume_status,
        "task_state": task_state.to_dict(),
        "last_request_metadata": request_metadata,
        "completion_usage_totals": usage,
        "session_messages_count": session_metrics["messages_count"],
        "session_messages_chars": session_metrics["messages_chars"],
        "session_messages_tokens": session_metrics["messages_tokens"],
        "session_tool_event_count": tool_metrics["event_count"],
        "session_tool_name_counts": tool_metrics["name_counts"],
        "session_tool_status_counts": tool_metrics["status_counts"],
        "working_memory": self.memory.to_dict(),
        "redacted_env": self.detected_secret_env_summary(),
    }
```

Do not read `history_text()` to compute any report field.

- [ ] **Step 4: Migrate report consumers with explicit field mappings**

Apply these mappings:

```text
pico/evaluation/metrics_reports.py:
  report.prompt_metadata.prompt_chars -> report.last_request_metadata.messages_chars
  report.prompt_metadata.cached_tokens -> report.completion_usage_totals.cached_tokens
  report.prompt_metadata.cache_hit -> report.completion_usage_totals.cache_hit
  report.prompt_metadata.input_tokens -> report.completion_usage_totals.input_tokens
  report.prompt_metadata.prefix_changed -> report.last_request_metadata.prefix_changed

pico/evaluation/provider_benchmark.py:
  prompt_metadata -> completion_usage_totals for cache values

pico/evaluation/experiments_recovery.py:
  report.prompt_metadata.resume_status -> report.last_request_metadata.resume_status

benchmarks/coding_tasks.json:
  report.prompt_metadata.resume_status -> report.last_request_metadata.resume_status
```

Rename the aggregate key `avg_prompt_chars` to `avg_request_messages_chars`. Update its tests and Markdown renderers to say “sent request message chars,” not “prompt chars.”

- [ ] **Step 5: Run the report/metrics slice**

```bash
uv run pytest tests/test_messages.py tests/test_runtime_report.py tests/test_metrics.py tests/test_metadata_completeness.py tests/test_run_store.py tests/test_evaluator.py -q
```

Expected: all pass.

- [ ] **Step 6: Verify production report consumers no longer read the old key**

```bash
rg -n 'report\.get\("prompt_metadata"|report\["prompt_metadata"\]' pico tests
```

Expected: no output.

- [ ] **Step 7: Commit report consumer migration**

```bash
git add pico/messages.py pico/runtime.py pico/evaluation/metrics_reports.py pico/evaluation/provider_benchmark.py pico/evaluation/experiments_recovery.py benchmarks/coding_tasks.json tests/test_messages.py tests/test_runtime_report.py tests/test_metrics.py tests/test_metadata_completeness.py tests/test_run_store.py
git commit -m "refactor(report): read canonical message and usage truth"
```

---

### Task 6: Replace CLI Drift Checking and Fixed-Benchmark History Reads

**Files:**

- Modify: `pico/cli_session.py`
- Modify: `pico/evaluation/fixed_benchmark.py`
- Modify: `tests/test_cli_session_inspect.py`
- Modify: `tests/test_evaluator.py`
- Modify: `tests/test_cli_diagnostics.py`

**Interfaces:**

- `inspect_session(session_id, sessions_root) -> tuple[bool, str]` remains stable.
- Its truth changes from history/messages turn-count comparison to messages schema/role/block/meta/pair validation.
- Fixed benchmark artifact field changes from `initial_history_empty` to `initial_messages_empty`.

- [ ] **Step 1: Replace drift tests with messages-invariant tests**

Replace `tests/test_cli_session_inspect.py` with cases shaped like:

```python
import json

from pico.cli_session import inspect_session


def _write_session(root, session_id, messages, schema_version=3):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{session_id}.json").write_text(
        json.dumps({
            "id": session_id,
            "schema_version": schema_version,
            "messages": messages,
        }),
        encoding="utf-8",
    )


def test_inspect_reports_schema_roles_blocks_pairs_and_meta(tmp_path):
    root = tmp_path / "sessions"
    messages = [
        {"role": "user", "content": "q", "_pico_meta": {"created_at": "t"}},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "_pico_meta": {"created_at": "t"},
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "body"}],
            "_pico_meta": {"created_at": "t"},
        },
        {"role": "assistant", "content": "done", "_pico_meta": {"created_at": "t"}},
    ]
    _write_session(root, "s1", messages)
    ok, report = inspect_session("s1", root)
    assert ok is True
    assert "schema_version: 3" in report
    assert "messages: 4" in report
    assert "role_sequence: user -> assistant -> user -> assistant" in report
    assert "tool_pairs: 1" in report
    assert "orphans: 0" in report
    assert "invariants: ok" in report


def test_inspect_fails_on_orphan_without_consulting_history(tmp_path):
    root = tmp_path / "sessions"
    _write_session(root, "bad", [{
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}}],
        "_pico_meta": {},
    }])
    ok, report = inspect_session("bad", root)
    assert ok is False
    assert "orphan" in report.lower()


def test_inspect_fails_when_v3_contains_history(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "bad-history.json").write_text(
        json.dumps({
            "id": "bad-history",
            "schema_version": 3,
            "messages": [],
            "history": [],
        }),
        encoding="utf-8",
    )
    ok, report = inspect_session("bad-history", root)
    assert ok is False
    assert "history" in report.lower()
```

Keep the existing missing-file and malformed-JSON cases.

- [ ] **Step 2: Confirm old drift logic fails the new tests**

```bash
uv run pytest tests/test_cli_session_inspect.py -q
```

Expected: failures because the report still compares history/user counts and does not validate pairs.

- [ ] **Step 3: Implement the messages-only inspector**

Replace the counting helpers in `pico/cli_session.py` with:

```python
from pico.messages import MessageValidationError, validate_messages


def _pair_count(messages):
    return sum(
        1
        for message in messages
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_use"
    )


def inspect_session(session_id, sessions_root):
    path = Path(sessions_root) / f"{session_id}.json"
    if not path.exists():
        return False, f"session not found: {path}"
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"failed to read session {session_id}: {exc}"

    version = session.get("schema_version", "unknown")
    messages = session.get("messages")
    lines = [
        f"session: {session_id}",
        f"schema_version: {version}",
        f"messages: {len(messages) if isinstance(messages, list) else 0}",
        "role_sequence: " + (
            " -> ".join(
                str(message.get("role", "?"))
                for message in messages
                if isinstance(message, dict)
            )
            if isinstance(messages, list)
            else "invalid"
        ),
    ]
    if version == 3 and "history" in session:
        lines.extend(["tool_pairs: 0", "orphans: unknown", "invariants: failed (v3 contains history)"])
        return False, "\n".join(lines)
    try:
        validate_messages(messages, require_meta=version == 3)
    except MessageValidationError as exc:
        lines.extend(["tool_pairs: 0", "orphans: 1", f"invariants: failed ({exc})"])
        return False, "\n".join(lines)
    lines.extend([
        f"tool_pairs: {_pair_count(messages)}",
        "orphans: 0",
        "invariants: ok",
    ])
    return True, "\n".join(lines)
```

- [ ] **Step 4: Rename the fixed-benchmark empty-state field**

In `pico/evaluation/fixed_benchmark.py`:

```python
initial_messages_empty = len(agent.session.get("messages", [])) == 0
```

After `agent.ask`, call `validate_messages(agent.session["messages"], require_meta=True)` and write `"message_invariants_valid": True` into the row. Write `"initial_messages_empty": initial_messages_empty` into the artifact and remove `initial_history_empty`. Update `tests/test_evaluator.py` to assert both new fields. Change CLI diagnostic session fixtures from history-only JSON to:

```json
{"id":"session_1","schema_version":3,"messages":[]}
```

- [ ] **Step 5: Run CLI and fixed-benchmark tests**

```bash
uv run pytest tests/test_cli_session_inspect.py tests/test_cli_diagnostics.py tests/test_evaluator.py -q
```

Expected: all pass.

- [ ] **Step 6: Verify migrated consumers do not read history**

```bash
rg -n 'session\["history"\]|get\("history"' pico/cli_session.py pico/evaluation/fixed_benchmark.py
```

Expected: no output. The category label `history_reference` may remain because it names a benchmark scenario, not a storage field.

- [ ] **Step 7: Commit CLI and fixed benchmark migration**

```bash
git add pico/cli_session.py pico/evaluation/fixed_benchmark.py tests/test_cli_session_inspect.py tests/test_evaluator.py tests/test_cli_diagnostics.py
git commit -m "refactor(consumers): inspect canonical messages only"
```

---

### Task 7: Make Context Evaluation Measure the Actual Request View

**Files:**

- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `pico/evaluation/experiments_real.py`
- Modify: `pico/evaluation/metrics_reports.py`
- Modify: `pico/evaluation/metrics_experiments.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_evaluator.py`

**Interfaces:**

- Context ablation compares `history_soft_cap=4_000` with `history_soft_cap=1_000_000`.
- It measures `request["messages"]` after turn-unit dropping, never `ContextManager.build()` or the `context_reduction` feature flag.
- Memory ablation remains skipped until Task 10 restores working-summary injection and its leakage precondition.

- [ ] **Step 1: Add a request-view ablation assertion**

Add to `tests/test_metrics.py`:

```python
def test_context_ablation_compares_bounded_and_unbounded_sent_messages(tmp_path):
    artifact = run_context_ablation_v2(
        artifact_path=tmp_path / "context.json",
        repetitions=1,
    )
    summary = artifact["summary"]
    assert summary["avg_bounded_request_chars"] < summary["avg_unbounded_request_chars"]
    assert summary["current_request_preserved_rate"] == 1.0
    assert all(
        config["bounded_dropped_messages"] > 0
        for config in artifact["configs"]
        if config["history_level"] == "long"
    )
```

- [ ] **Step 2: Confirm the legacy string-prompt experiment fails**

```bash
uv run pytest tests/test_metrics.py::test_context_ablation_compares_bounded_and_unbounded_sent_messages -q
```

Expected: failure because the artifact still reports `avg_full_prompt_chars` and `avg_raw_prompt_chars` from the legacy builder.

- [ ] **Step 3: Add canonical test-message seeding and request measurement**

Replace `measure_feature_ablation_metrics` and direct `agent.record` setup in `pico/evaluation/experiments_synthetic.py` with:

```python
from ..messages import message_content_text


def _seed_plain_messages(agent, count, prefix, payload_size):
    seeded = []
    for index in range(int(count)):
        seeded.append({
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"{prefix}-{index}-" + ("X" * int(payload_size)),
            "_pico_meta": {"created_at": f"2026-04-08T11:{index:02d}:00+00:00"},
        })
    agent.session["messages"].extend(seeded)


def _sent_message_chars(request):
    return sum(len(message_content_text(message)) for message in request["messages"])


def _latest_plain_user(request):
    for message in reversed(request["messages"]):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def measure_request_ablation_metrics(agent, user_message):
    original_cap = agent.context_config["history_soft_cap"]
    results = {}
    try:
        for name, cap in (("bounded", 4_000), ("unbounded", 1_000_000)):
            agent.context_config["history_soft_cap"] = cap
            request, metadata = agent.context_manager.build_v2(user_message)
            results[name] = {
                "request_chars": _sent_message_chars(request),
                "dropped_messages": int(metadata["dropped_messages"]),
                "current_request_preserved": user_message in _latest_plain_user(request),
            }
    finally:
        agent.context_config["history_soft_cap"] = original_cap
    return results
```

Update `build_stress_agent_metrics` and `run_context_stress_matrix` to call `_seed_plain_messages` and `measure_request_ablation_metrics`. Use these artifact keys:

```python
{
    "bounded_request_chars": bounded["request_chars"],
    "unbounded_request_chars": unbounded["request_chars"],
    "bounded_dropped_messages": bounded["dropped_messages"],
    "compression_ratio": _safe_ratio(
        unbounded["request_chars"] - bounded["request_chars"],
        unbounded["request_chars"],
    ),
    "current_request_preserved": bounded["current_request_preserved"],
}
```

Summary keys are `avg_bounded_request_chars`, `avg_unbounded_request_chars`, `avg_request_compression_ratio`, and `current_request_preserved_rate`.

- [ ] **Step 4: Migrate the real-evaluation message manipulation**

In `pico/evaluation/experiments_real.py`:

1. Replace `_inject_memory_noise` with `_seed_plain_messages(agent, rounds, "filler-turn", 560)`.
2. Replace `_truncate_read_history` with a copy-on-write message transform:

```python
def _truncate_read_messages(agent):
    updated = []
    for message in agent.session.get("messages", []):
        replacement = dict(message)
        content = message.get("content")
        if (
            message.get("role") == "user"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_result"
        ):
            block = dict(content[0])
            block["content"] = "(truncated from transcript)"
            replacement["content"] = [block]
        updated.append(replacement)
    agent.session["messages"] = updated
    agent.session_path = agent.session_store.save(agent.session)
```

3. Replace `context_reduction=True/False` variants with the same `4_000` versus `1_000_000` `history_soft_cap` values.
4. Rename real context artifact/report fields to the bounded/unbounded names from Step 3.

- [ ] **Step 5: Update report rendering terminology**

In `pico/evaluation/metrics_reports.py`, replace “full/no context reduction prompt chars” with “bounded/unbounded sent-message chars” and read:

```python
context["summary"]["avg_bounded_request_chars"]
context["summary"]["avg_unbounded_request_chars"]
```

Do not retain aliases with the old `prompt_chars` names.

- [ ] **Step 6: Run the Phase 2 consumer gate**

```bash
uv run pytest tests/test_metrics.py tests/test_evaluator.py tests/test_cli_session_inspect.py tests/test_runtime_report.py -q
rg -n 'session\["history"\]|get\("history"|_build_prompt_and_metadata' pico/evaluation pico/cli_session.py
```

Expected: tests pass; the search has no production consumer hits. `history_reference` category names and migration-specific `history` reads are not included in this search scope.

- [ ] **Step 7: Commit actual-request evaluation**

```bash
git add pico/evaluation/experiments_synthetic.py pico/evaluation/experiments_real.py pico/evaluation/metrics_reports.py pico/evaluation/metrics_experiments.py tests/test_metrics.py tests/test_evaluator.py
git commit -m "refactor(evaluation): measure sent message views"
```

---

### Task 8: Atomically Switch Native and Fallback Paths to the Action Codec

**Files:**

- Modify: `pico/agent_loop.py`
- Modify: `pico/prompt_prefix.py`
- Modify: `pico/providers/fallback_adapter.py`
- Modify: `pico/providers/anthropic_compatible.py`
- Modify: `pico/runtime.py`
- Modify: `pico/evaluation/experiments_recovery.py`
- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_prompt_prefix.py`
- Modify: `tests/test_provider_fallback.py`
- Modify: `tests/test_provider_anthropic_v2.py`
- Modify: `tests/e2e/test_fallback_provider_parity.py`
- Modify: `tests/test_runtime_report.py`
- Test: `tests/test_action_codec.py`

**Interfaces:**

- `FallbackAdapter.complete_v2(system, tools, messages, max_tokens, cache_breakpoints) -> Response(END_TURN, raw text, inner usage)`.
- `AgentLoop` consumes only `ToolAction | FinalAction | RetryAction` after `decode_action`.
- Every `model_turn` has `request_metadata` and `completion_usage` copied from that exact call.
- Final report receives aggregates for input/output/total/cached/cache-create/cache-read and any cache hit.
- This task is one commit. Do not commit or hand off after changing only Fallback or only AgentLoop.

- [ ] **Step 1: Add atomic-switch integration tests**

Add a native scripted Provider to `tests/test_agent_loop.py`:

```python
class NativeScriptProvider:
    supports_prompt_cache = True
    supports_native_tools = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_completion_metadata = {"input_tokens": 999999}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({
            "system": system,
            "tools": tools,
            "messages": messages,
            "max_tokens": max_tokens,
            "cache_breakpoints": cache_breakpoints,
        })
        return self.responses.pop(0)


def build_native_agent(tmp_path, provider, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )
```

Add:

```python
def test_agent_loop_decodes_native_action_and_aggregates_response_usage_only(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "toolu_native", "name": "read_file", "input": {"path": "README.md"}}],
            usage={
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "cached_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 3,
                "cache_hit": True,
            },
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        ),
    ])
    agent = Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )
    assert agent.ask("read and finish") == "done"

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    decoded = [event for event in events if event["event"] == "action_decoded"]
    turns = [event for event in events if event["event"] == "model_turn"]
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert decoded[0]["action_type"] == "tool"
    assert decoded[0]["origin"] == "native_tool_use"
    assert decoded[1]["action_type"] == "final"
    assert [turn["completion_usage"]["input_tokens"] for turn in turns] == [10, 20]
    assert report["completion_usage_totals"]["input_tokens"] == 30
    assert report["completion_usage_totals"]["output_tokens"] == 7
    assert report["completion_usage_totals"]["total_tokens"] == 37
    assert report["completion_usage_totals"]["cache_hit"] is True
    assert report["completion_usage_totals"]["input_tokens"] != 999999
```

Update `tests/test_provider_fallback.py` so raw text is preserved:

```python
def test_fallback_returns_raw_text_and_usage_for_the_shared_codec():
    raw = '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>'
    inner = _StubInner(raw)
    response = FallbackAdapter(inner).complete_v2(
        system=[{"type": "text", "text": "SYSTEM"}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {}}],
        messages=[{"role": "user", "content": "q"}],
        max_tokens=100,
    )
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [{"type": "text", "text": raw}]
    assert response.usage == {"input_tokens": 3, "output_tokens": 2}
    assert inner.last_prompt.count("Text response protocol:") == 1
```

Add a prefix assertion:

```python
def test_stable_prefix_is_native_tool_protocol_neutral(workspace, tools):
    prefix = build_prompt_prefix(workspace, tools).text
    assert "Return exactly one <tool>" not in prefix
    assert "<final>" not in prefix
    assert '<tool>{"name":' not in prefix
```

- [ ] **Step 2: Confirm the focused switch tests fail**

```bash
uv run pytest tests/test_agent_loop.py::test_agent_loop_decodes_native_action_and_aggregates_response_usage_only tests/test_provider_fallback.py::test_fallback_returns_raw_text_and_usage_for_the_shared_codec tests/test_prompt_prefix.py -q
```

Expected: failures because Fallback parses text, prefix forces XML, `action_decoded` does not exist, and usage comes from the Provider side channel.

- [ ] **Step 3: Make the stable prefix protocol-neutral**

In `pico/prompt_prefix.py`:

- Remove `TOOL_EXAMPLES`, `TOOL_EXAMPLE_ORDER`, and `_response_examples`.
- Remove XML formatting lines from `_tool_specific_rules`.
- Retain tool schemas, approval descriptions, edit preference, required-argument guidance, and workspace stable text.
- Use this rules core:

```python
Rules:
- Use the provided native tools instead of guessing about the workspace.
- Never invent tool results.
- Keep answers concise and concrete.
- Before writing tests for existing code, read the implementation first.
- When writing tests, match the current implementation unless the user explicitly asked you to change the code.
- New files should be complete and runnable, including obvious imports.
- Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
```

Do not mention `<tool>` or `<final>` anywhere in the stable prefix.

- [ ] **Step 4: Make Fallback flattening the sole text-protocol instructor**

In `pico/providers/fallback_adapter.py`, remove `uuid` and `parse_model_output`. Import `render_transcript` and `strip_pico_meta` from `pico.messages`. Define:

```python
_TEXT_PROTOCOL_INSTRUCTION = """Text response protocol:
Return exactly one action.
For a tool call, use strict JSON:
<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
For a final answer, use:
<final>answer</final>
Do not wrap examples or explanations around the action."""
```

Replace `complete_v2` with:

```python
def complete_v2(
    self,
    *,
    system,
    tools,
    messages,
    max_tokens,
    cache_breakpoints=None,
):
    del cache_breakpoints
    clean_messages = strip_pico_meta(messages)
    prompt = "\n\n".join(
        part
        for part in (
            _flatten_system(system),
            _flatten_tools(tools),
            _TEXT_PROTOCOL_INSTRUCTION,
            render_transcript(clean_messages),
        )
        if part
    )
    raw = self._inner.complete(prompt, max_tokens)
    usage = dict(getattr(self._inner, "last_completion_metadata", {}) or {})
    self.last_completion_metadata = usage
    return Response(
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": str(raw)}],
        usage=usage,
    )
```

Fallback may read the legacy inner Provider metadata because it must translate the legacy return type into `Response.usage`. No runtime consumer may read it afterward.

- [ ] **Step 5: Map unknown native stop reasons honestly**

In `pico/providers/anthropic_compatible.py`:

```python
stop_reason = stop_map.get(data.get("stop_reason"), StopReason.UNKNOWN)
```

Import `strip_pico_meta` from `pico.messages` and delete the local import from `providers.message_utils`. Add an Anthropic test where an unknown wire stop reason produces `StopReason.UNKNOWN`.

- [ ] **Step 6: Add action trace and usage aggregation helpers**

In `pico/agent_loop.py` import `FinalAction`, `RetryAction`, `ToolAction`, and `decode_action`. Add:

```python
_USAGE_SUM_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _empty_usage_totals():
    return {**{key: 0 for key in _USAGE_SUM_KEYS}, "cache_hit": False}


def _add_usage(totals, usage):
    usage = dict(usage or {})
    for key in _USAGE_SUM_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            totals[key] += value
    if "total_tokens" not in usage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            totals["total_tokens"] += input_tokens + output_tokens
    totals["cache_hit"] = totals["cache_hit"] or bool(usage.get("cache_hit"))
    return totals


def _action_trace_payload(action):
    if isinstance(action, ToolAction):
        return {
            "action_type": "tool",
            "origin": action.origin,
            "ignored_tool_count": action.ignored_tool_count,
        }
    if isinstance(action, FinalAction):
        return {
            "action_type": "final",
            "origin": action.origin,
            "truncated": action.truncated,
        }
    return {
        "action_type": "retry",
        "origin": action.origin,
        "reason_code": action.reason_code,
        "excerpt": action.excerpt,
    }
```

- [ ] **Step 7: Replace Response block interpretation with `decode_action`**

At the start of `AgentLoop.run` create:

```python
completion_usage_totals = _empty_usage_totals()
```

After each successful `complete_v2`:

```python
completion_usage = dict(raw_response.usage or {})
_add_usage(completion_usage_totals, completion_usage)
agent.last_prompt_metadata = dict(prompt_metadata)
action = decode_action(raw_response)
action_payload = _action_trace_payload(action)
agent.emit_trace(
    task_state,
    "action_decoded",
    {
        **action_payload,
        "request_metadata": prompt_metadata,
    },
)
agent.emit_trace(
    task_state,
    "model_turn",
    {
        "attempts": task_state.attempts,
        "tool_steps": task_state.tool_steps,
        "stop_reason": str(
            getattr(raw_response.stop_reason, "value", raw_response.stop_reason)
        ),
        "request_metadata": prompt_metadata,
        "completion_usage": completion_usage,
        **action_payload,
        "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
    },
)
```

Delete all code that filters `text_blocks`/`tool_use_blocks`, computes `kind`, emits `model_parsed`, reads `agent.model_client.last_completion_metadata`, or merges completion values into request metadata.

Apply actions with these branches:

```python
if isinstance(action, ToolAction):
    name = action.name
    args = action.arguments
    tool_use_id = action.tool_use_id
    # Continue through the existing tool execution path.

elif isinstance(action, RetryAction):
    agent.record({
        "role": "assistant",
        "content": action.notice,
        "created_at": now(),
    })
    agent.run_store.write_task_state(task_state)
    continue

else:
    final = action.text
    # Continue through the existing assistant-final and finish path.
```

The retry history write is intentionally temporary and is removed by the frozen request/feedback work in Task 9.

Pass `completion_usage_totals=completion_usage_totals` through every call to `_finish_run` and then to:

```python
agent.build_report(
    task_state,
    completion_usage_totals=completion_usage_totals,
)
```

- [ ] **Step 8: Run both paths together**

```bash
uv run pytest tests/test_action_codec.py tests/test_provider_fallback.py tests/test_provider_anthropic_v2.py tests/test_prompt_prefix.py tests/test_agent_loop.py tests/e2e/test_fallback_provider_parity.py tests/test_runtime_report.py -q
```

Expected: all pass. Both native and Fallback tool turns execute successfully through `decode_action`.

- [ ] **Step 9: Prove the runtime side channel is gone**

```bash
rg -n 'last_completion_metadata' pico/agent_loop.py pico/runtime.py pico/evaluation benchmarks/live_e2e
```

Expected at this phase: no hit in `pico/agent_loop.py` or report/evaluation code. Hits may remain only inside Provider adapters and the live harness scheduled for Task 16.

- [ ] **Step 10: Commit the atomic switch**

```bash
git add pico/agent_loop.py pico/prompt_prefix.py pico/providers/fallback_adapter.py pico/providers/anthropic_compatible.py pico/runtime.py pico/evaluation/experiments_recovery.py pico/evaluation/experiments_synthetic.py tests/test_agent_loop.py tests/test_prompt_prefix.py tests/test_provider_fallback.py tests/test_provider_anthropic_v2.py tests/e2e/test_fallback_provider_parity.py tests/test_runtime_report.py
git commit -m "refactor(action-kernel): route all model output through codec"
```

---

### Task 9: Freeze Turn Preflight, Injection, and One-Shot Feedback

**Files:**

- Modify: `pico/agent_loop.py`
- Modify: `pico/context_manager.py`
- Modify: `pico/context/intent.py`
- Modify: `pico/prompt_prefix.py`
- Modify: `pico/runtime.py`
- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `pico/evaluation/experiments_real.py`
- Modify: `pico/evaluation/fixed_benchmark.py`
- Modify: `benchmarks/coding_tasks.json`
- Modify: `benchmarks/perf/bench_build_v2.py`
- Modify: `tests/test_allowed_tools.py`
- Modify: `tests/test_clean_up.py`
- Modify: `tests/test_config_context.py`
- Modify: `tests/test_context_history_budget.py`
- Modify: `tests/test_context_intent.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_context_manager_v2.py`
- Modify: `tests/test_context_manager_injection.py`
- Modify: `tests/test_agent_loop_injection_sent.py`
- Modify: `tests/test_message_invariants.py`
- Modify: `tests/test_metadata_completeness.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_p2_smoke.py`
- Modify: `tests/test_pico.py`
- Modify: `tests/test_prompt_prefix.py`
- Modify: `tests/test_run_store.py`
- Modify: `tests/test_runtime_report.py`
- Delete: `tests/memory/test_prompt_layout.py`
- Test: `tests/test_evaluator.py`

**Interfaces:**

`ContextManager.build_v2(*, injection_snapshot, injection_telemetry, preflight_metadata, runtime_feedback="") -> tuple[dict, dict]`.

`AgentLoop` renders `render_current_user_message(agent, user_message)` exactly once after preflight and reuses the result for every attempt in that top-level turn.

Every direct `build_v2` consumer must first make its current plain user message the final canonical request-view message, then render its snapshot and call the keyword-only API.  A measurement preview that immediately calls `agent.ask(user_message)` must put that temporary message on a copied/restored session view, so `ask()` remains the sole persistent user-turn writer.  The fixed benchmark must seed canonical `messages` and use `history_soft_cap` / `history_floor_messages`, not legacy `history`, `total_budget`, or `section_budgets`.

- [ ] **Step 1: Add a deterministic three-attempt snapshot test**

Add to `tests/test_agent_loop_injection_sent.py`:

```python
def test_one_snapshot_survives_retry_and_tool_step_while_feedback_is_one_shot(
    tmp_path,
    monkeypatch,
):
    render_calls = []

    def frozen_render(agent, user_message):
        render_calls.append(user_message)
        return (
            "<system-reminder><pico:memory_index>SNAPSHOT</pico:memory_index></system-reminder>\n"
            + user_message,
            {
                "intent": {"name": "default", "matched_keyword": "", "matched_reason": "test"},
                "injection_tokens": {"memory_index": 1},
                "injection_truncated": {},
                "injection_dropped": [],
                "injection_budget": 100,
            },
        )

    monkeypatch.setattr("pico.agent_loop.render_current_user_message", frozen_render)
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{bad}</tool>"}],
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "README.md"}}],
            usage={"input_tokens": 2, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 3, "output_tokens": 1},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    assert agent.ask("inspect") == "done"
    assert render_calls == ["inspect"]

    sent = []
    for call in provider.calls:
        current = next(
            message["content"]
            for message in reversed(call["messages"])
            if message["role"] == "user" and isinstance(message["content"], str)
        )
        sent.append(current)
    assert all("SNAPSHOT" in content for content in sent)
    assert "runtime_feedback" not in sent[0]
    assert "runtime_feedback" in sent[1]
    assert "runtime_feedback" not in sent[2]
    assert len({call["system"][0]["text"] for call in provider.calls}) == 1

    canonical_text = json.dumps(agent.session["messages"])
    assert "SNAPSHOT" not in canonical_text
    assert "runtime_feedback" not in canonical_text
```

Use the existing native Provider fixture from Task 8; add `build_native_agent` as a small test helper if that file does not already have one.

- [ ] **Step 2: Add metadata-contract and context-reduction tests**

In `tests/test_metadata_completeness.py` assert every request has:

```python
required = {
    "system_cache_key",
    "system_tokens",
    "tools_tokens",
    "prompt_cache_supported",
    "messages_count",
    "messages_chars",
    "messages_tokens",
    "dropped_messages",
    "cache_control_breakpoints",
    "runtime_feedback_present",
    "intent",
    "injection_tokens",
    "injection_truncated",
    "injection_dropped",
    "injection_budget",
    "prefix_chars",
    "workspace_changed",
    "prefix_changed",
    "workspace_fingerprint",
    "tool_signature",
    "resume_status",
    "request_chars",
    "tool_count",
    "workspace_docs",
    "recent_commits",
}
forbidden = {
    "prompt_chars",
    "sections",
    "section_order",
    "section_budgets",
    "budget_reductions",
    "history_chars",
    "prompt_cache_key",
}
assert required <= metadata.keys()
assert forbidden.isdisjoint(metadata)
```

Rewrite the context-reduction runtime test to seed enough canonical plain turns to exceed `history_soft_cap`, then assert `dropped_messages > 0` and one `checkpoint_created` trace with `trigger == "context_reduction"`.

Update `tests/test_context_intent.py` so the budget-key contract includes
`"checkpoint"`, and revise the module prose from four sources to five. Update
the empty-output and malformed-tool retry tests in `tests/test_pico.py` to
assert that the retry notice is absent from canonical/legacy history and is
present only in the immediately following provider request:

```python
notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
assert not any("empty response" in item for item in notices)
assert "<pico:runtime_feedback>" in agent.model_client.prompts[1]
assert "empty response" in agent.model_client.prompts[1]
```

- [ ] **Step 3: Confirm snapshot and metadata tests fail**

```bash
uv run pytest tests/test_agent_loop_injection_sent.py tests/test_metadata_completeness.py tests/test_runtime_report.py -q
```

Expected: snapshot is re-rendered or absent after tool_result, feedback is not sent, and legacy metadata keys remain.

- [ ] **Step 4: Give resume checkpoints a real injection budget**

Add `"checkpoint"` to every profile budget in `pico/context/intent.py`:

```python
"debug": 600
"recall": 800
"structural": 500
"default": 500
```

Keep the existing budgets for other sources unchanged.

- [ ] **Step 5: Add explicit turn preflight**

In `pico/agent_loop.py` import `render_current_user_message` and add:

```python
def _run_turn_preflight(agent, user_message):
    refresh = agent.refresh_prefix()
    agent.resume_state = agent.evaluate_resume_state()
    metadata = {
        "prefix_chars": len(agent.prefix),
        "workspace_chars": len(agent.workspace.text()),
        "memory_chars": len(agent.memory_text()),
        "request_chars": len(str(user_message)),
        "tool_count": len(agent.tools),
        "workspace_docs": len(agent.workspace.project_docs),
        "recent_commits": len(agent.workspace.recent_commits),
        "workspace_fingerprint": agent.prefix_state.workspace_fingerprint,
        "tool_signature": agent.prefix_state.tool_signature,
        "workspace_changed": refresh["workspace_changed"],
        "prefix_changed": refresh["prefix_changed"],
        "resume_status": agent.resume_state.get("status", CHECKPOINT_NONE_STATUS),
        "stale_summary_invalidations": int(
            agent.resume_state.get("stale_summary_invalidations", 0)
        ),
        "stale_paths": list(agent.resume_state.get("stale_paths", [])),
        "runtime_identity_mismatch_fields": list(
            agent.resume_state.get("runtime_identity_mismatch_fields", [])
        ),
    }
    metadata.update(agent.detected_secret_env_summary())
    return metadata
```

After user-message persistence and `run_started`, but before the attempt loop:

```python
preflight_metadata = _run_turn_preflight(agent, user_message)
injection_snapshot, injection_telemetry = render_current_user_message(
    agent,
    user_message,
)
pending_runtime_feedback = ""
context_reduction_checkpoint_created = False
```

Do not call preflight or the renderer again inside the attempt loop.

- [ ] **Step 6: Make `build_v2` assemble only the actual request**

In `pico/context_manager.py` import `build_request_messages`, `message_content_text`, and `message_metrics` from `pico.messages`. Change the signature to the interface above. Replace the old user-message render/substitution block with:

```python
session = getattr(self.agent, "session", {}) or {}
messages = build_request_messages(
    list(session.get("messages", []) or []),
    rendered_user=injection_snapshot,
    runtime_feedback=runtime_feedback,
)
runtime_feedback_present = bool(str(runtime_feedback or "").strip())
```

Keep the existing pinned system/tools hard-cap check and turn-unit dropping. Use `message_content_text` in the drop estimator. After dropping, calculate:

```python
metrics = message_metrics(messages, token_of=self._count_tokens_for_v2)
metadata = {
    "system_cache_key": hashlib.sha256(system_text.encode("utf-8")).hexdigest(),
    "system_tokens": system_tokens,
    "tools_tokens": tools_tokens,
    "prompt_cache_supported": bool(
        getattr(self.agent.model_client, "supports_prompt_cache", False)
    ),
    **metrics,
    "dropped_messages": dropped_messages,
    "cache_control_breakpoints": list(breakpoints),
    "runtime_feedback_present": runtime_feedback_present,
    "recall.error_count": int(recall_errors.get("count", 0) or 0),
    "recall.last_error": str(recall_errors.get("last", "") or ""),
    **dict(injection_telemetry),
    **dict(preflight_metadata),
}
```

Delete the `prompt_cache_key` alias. `messages_count/chars/tokens` must be computed after dropping and must describe exactly `request["messages"]`.

- [ ] **Step 7: Consume feedback only after successful request construction**

Replace both legacy prompt builds in the attempt loop with:

```python
request, request_metadata = agent.context_manager.build_v2(
    injection_snapshot=injection_snapshot,
    injection_telemetry=injection_telemetry,
    preflight_metadata=preflight_metadata,
    runtime_feedback=pending_runtime_feedback,
)
pending_runtime_feedback = ""
agent.last_prompt_metadata = dict(request_metadata)
```

If `request_metadata["dropped_messages"] > 0` and the turn has not already created this checkpoint:

```python
_create_resume_checkpoint(
    agent,
    task_state,
    user_message,
    trigger="context_reduction",
)
context_reduction_checkpoint_created = True
```

For `RetryAction`, replace the temporary history write with:

```python
pending_runtime_feedback = action.notice
agent.run_store.write_task_state(task_state)
continue
```

Emit the same `request_metadata` object in `prompt_built`, `model_requested`, `action_decoded`, and `model_turn`. Do not emit a `prompt_cache_key` sibling.
For `prompt_built`, the payload is specifically
`{"request_metadata": request_metadata, "duration_ms": ...}`; update all
trace assertions that still read `prompt_metadata`.

- [ ] **Step 8: Move memory guidance into the stable prefix**

Move `MEMORY_USAGE_GUIDANCE` and `MEMORY_READING_GUIDANCE` verbatim from `pico/context_manager.py` into `pico/prompt_prefix.py`. Append both once, immediately before `workspace.stable_text()` in `build_prompt_prefix`. Add tests that each opening tag occurs exactly once in the stable prefix and never in the current user request.

- [ ] **Step 9: Delete the legacy string-builder surface**

Delete these production symbols now that no runtime call remains:

```text
pico/context_manager.py:
  DEFAULT_TOTAL_BUDGET
  DEFAULT_SECTION_BUDGETS
  DEFAULT_SECTION_FLOORS
  DEFAULT_REDUCTION_ORDER
  SECTION_ORDER
  CURRENT_REQUEST_SECTION
  SectionRender
  ContextManager.build
  ContextManager._render_sections_without_reduction
  ContextManager._compute_section_floors
  ContextManager._render_sections
  ContextManager._render_history_section
  ContextManager._compressed_history_entries
  ContextManager._reusable_file_summary
  ContextManager._summarize_old_tool_item
  ContextManager._raw_history_text
  ContextManager._history_head_parts
  ContextManager._render_history_head
  ContextManager._clip_workspace_state
  ContextManager._empty_transcript_text
  ContextManager._history_section_raw
  ContextManager._render_history_item
  ContextManager._assemble_prompt
  ContextManager._metadata

pico/runtime.py:
  Pico.prompt
  Pico.prompt_metadata
  Pico._build_prompt_and_metadata
```

Reduce `ContextManager.__init__` to `def __init__(self, agent): self.agent = agent` and update tests that supplied legacy section budgets/floors. The request-view caps continue to come from `agent.context_config`.

Delete `tests/memory/test_prompt_layout.py` because every assertion is against the removed string layout. Rewrite `tests/test_context_manager.py` to retain only pinned-cap, tool-schema, cache-key, and message-drop behavior against `build_v2`. Update every direct `build_v2(user_message)` call and `benchmarks/perf/bench_build_v2.py` to render one snapshot first and call the new keyword-only signature.

For the direct request-view helpers in `experiments_synthetic.py`, `experiments_real.py`, and `bench_build_v2.py`, seed a final canonical plain user message before rendering.  In `experiments_real.py`, remove that preview-only message after computing the request metrics and before calling `agent.ask(user_message)`.  Migrate the fixed `context_reduction_checkpoint` fixture in both `fixed_benchmark.py` and `benchmarks/coding_tasks.json` to canonical messages plus a small `history_soft_cap`; retain the benchmark's assertion that a real request-view drop creates the checkpoint.  Replace removed `Pico.prompt` tests with stable-prefix or actual-request assertions, and update the generic trace fixture to use `request_metadata` / `request_chars`.

- [ ] **Step 10: Remove the production context-reduction flag**

Delete `"context_reduction": True` from `DEFAULT_FEATURE_FLAGS`. Delete every production `feature_enabled("context_reduction")` branch. Keep `history_soft_cap` and `history_floor_messages` config because they control the real request view.

- [ ] **Step 11: Run the request-loop convergence gate**

```bash
uv run pytest tests/test_agent_loop_injection_sent.py tests/test_allowed_tools.py tests/test_clean_up.py tests/test_config_context.py tests/test_context_history_budget.py tests/test_context_intent.py tests/test_context_manager.py tests/test_context_manager_v2.py tests/test_context_manager_injection.py tests/test_message_invariants.py tests/test_metadata_completeness.py tests/test_metrics.py tests/test_p2_smoke.py tests/test_pico.py tests/test_prompt_prefix.py tests/test_run_store.py tests/test_runtime_report.py tests/test_evaluator.py -q
uv run python -m benchmarks.perf.bench_build_v2
```

Expected: tests pass; benchmark emits valid JSON; the three-attempt test sees one snapshot and one feedback occurrence.

- [ ] **Step 12: Run structural checks for removed request paths**

```bash
rg -n 'ContextManager\.build\(|_build_prompt_and_metadata|\bbudget_reductions\b|\bprompt_chars\b|\bsection_order\b|\bsection_budgets\b|\bhistory_chars\b|feature_enabled\("context_reduction"\)|"context_reduction":' pico benchmarks tests --glob '!benchmarks/results/**' --glob '!benchmarks/live_e2e/**'
```

Expected: no production or active-test hits. Stored benchmark artifacts and the live harness are excluded because they are historical data and Task 16's dedicated migration scope, respectively.

- [ ] **Step 13: Commit request-loop convergence**

```bash
git add pico/agent_loop.py pico/context_manager.py pico/context/intent.py pico/prompt_prefix.py pico/runtime.py pico/evaluation/experiments_synthetic.py pico/evaluation/experiments_real.py pico/evaluation/fixed_benchmark.py benchmarks/coding_tasks.json benchmarks/perf/bench_build_v2.py tests/test_agent_loop_injection_sent.py tests/test_allowed_tools.py tests/test_clean_up.py tests/test_config_context.py tests/test_context_history_budget.py tests/test_context_intent.py tests/test_context_manager.py tests/test_context_manager_v2.py tests/test_context_manager_injection.py tests/test_message_invariants.py tests/test_metadata_completeness.py tests/test_metrics.py tests/test_p2_smoke.py tests/test_pico.py tests/test_prompt_prefix.py tests/test_run_store.py tests/test_runtime_report.py tests/memory/test_prompt_layout.py
git commit -m "refactor(context): freeze turn request view"
```

---

### Task 10: Inject Recent Working Summaries and Repair Memory Ablation

**Files:**

- Modify: `pico/context/sources.py`
- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `pico/evaluation/experiments_real.py`
- Modify: `pico/evaluation/metrics_experiments.py`
- Modify: `tests/test_context_sources.py`
- Modify: `tests/test_agent_loop_injection_sent.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_memory_quality_benchmark.py`

**Interfaces:**

- `render_memory_index(agent, budget_tokens) -> str | None` keeps its signature.
- Output may contain durable `Memory files:` and/or `Recent working file summaries:`.
- Working summaries are included only when the existing `memory` feature is enabled and only for paths in `agent.memory.recent_files`.
- If a working summary exists, its marker and line are reserved ahead of durable memory entries so a full durable index cannot suppress it.
- Memory-ablation rows add `bootstrap_tool_turn_dropped: bool`.

- [ ] **Step 1: Add source and lifecycle tests**

Add to `tests/test_context_sources.py`:

```python
def test_memory_index_renders_recent_summary_without_durable_entries(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )
    text = render_memory_index(agent, budget_tokens=200)
    assert "Recent working file summaries:" in text
    assert "README.md -> project entry point" in text


def test_memory_index_omits_working_summaries_when_memory_is_off(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )
    agent.feature_flags["memory"] = False
    text = render_memory_index(agent, budget_tokens=200)
    assert text is None or "Recent working file summaries:" not in text


def test_memory_index_reserves_working_summary_when_durable_index_overflows(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )
    agent.memory_store = MagicMock()
    agent.memory_store.list.return_value = [
        MagicMock(path=f"notes/{index}.md", size_chars=1000, first_line="x" * 80)
        for index in range(30)
    ]

    text = render_memory_index(agent, budget_tokens=60)

    assert "Recent working file summaries:" in text
    assert "README.md -> project entry point" in text
```

Add a two-turn test to `tests/test_agent_loop_injection_sent.py`:

```python
def test_tool_created_summary_appears_next_top_level_turn_not_current_turn(tmp_path):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "README.md"}}],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "first done"}],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "second done"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    assert agent.ask("read README") == "first done"
    assert agent.ask("what did README say?") == "second done"
    current_users = [
        next(
            message["content"]
            for message in reversed(call["messages"])
            if message["role"] == "user" and isinstance(message["content"], str)
        )
        for call in provider.calls
    ]
    marker = "Recent working file summaries:"
    assert marker not in current_users[0]
    assert marker not in current_users[1]
    assert marker in current_users[2]
```

- [ ] **Step 2: Confirm working summaries are currently absent**

```bash
uv run pytest tests/test_context_sources.py tests/test_agent_loop_injection_sent.py::test_tool_created_summary_appears_next_top_level_turn_not_current_turn -q
```

Expected: failures because `render_memory_index` returns early when durable entries are empty and ignores session file summaries.

- [ ] **Step 3: Extend the existing memory-index source**

Replace `render_memory_index` in `pico/context/sources.py` with:

```python
def render_memory_index(agent, budget_tokens):
    entries = []
    store = getattr(agent, "memory_store", None)
    if store is not None:
        try:
            entries = list(store.list() or [])
        except Exception as exc:
            logger.debug("memory_index source failed: %s", exc)

    durable_lines = []
    if entries:
        durable_lines.append("Memory files:")
        for entry in entries:
            first = (getattr(entry, "first_line", "") or "")[:80]
            durable_lines.append(
                f"- {entry.path} ({entry.size_chars} chars) {first}"
            )

    memory_enabled = True
    feature_enabled = getattr(agent, "feature_enabled", None)
    if callable(feature_enabled):
        memory_enabled = bool(feature_enabled("memory"))
    if memory_enabled:
        recent_files = list(getattr(getattr(agent, "memory", None), "recent_files", []) or [])
        session = getattr(agent, "session", {}) or {}
        memory_state = session.get("memory", {}) if isinstance(session, dict) else {}
        summaries = memory_state.get("file_summaries", {}) if isinstance(memory_state, dict) else {}
        working_lines = []
        for path in recent_files:
            value = summaries.get(path)
            summary = value.get("summary", "") if isinstance(value, dict) else value
            summary = str(summary or "").strip()
            if summary:
                working_lines.append(f"{path} -> {summary}")
        if working_lines:
            working_text = "\n".join(
                ["Recent working file summaries:", *working_lines]
            )
        else:
            working_text = ""
    else:
        working_text = ""

    durable_text = "\n".join(durable_lines)
    if not durable_text and not working_text:
        return None
    char_budget = _budget_to_chars(budget_tokens)
    if not working_text:
        return _tail_clip(durable_text, char_budget)
    if len(working_text) >= char_budget:
        return _tail_clip(working_text, char_budget)
    durable_budget = char_budget - len(working_text) - 2
    if durable_text and durable_budget > 3:
        return _tail_clip(durable_text, durable_budget) + "\n\n" + working_text
    return working_text
```

Do not add a source, config key, or store read.

- [ ] **Step 4: Make all memory variants exercise the same dropped bootstrap**

In `pico/evaluation/experiments_synthetic.py`:

- Rename `_age_bootstrap_read_history` to `_age_bootstrap_messages`.
- Seed alternating user/assistant canonical messages with `_pico_meta`.
- Set `agent.context_config["history_soft_cap"] = 900` and `history_floor_messages = 2` before the follow-up.
- Capture the bootstrap tool-use id from canonical messages before adding filler.
- Make `memory_irrelevant` call both:

```python
memorylib.set_file_summary_dict(
    summaries,
    "other.txt",
    "the team mascot is blue",
    workspace_root=agent.root,
)
agent.memory.remember_file("other.txt")
agent._sync_working_memory()
```

- Make `memory_off` clear summaries and disable the feature.
- Record whether the actual follow-up Fallback prompt contains the captured bootstrap tool id:

```python
bootstrap_tool_turn_dropped = bootstrap_tool_use_id not in model_client.followup_prompt
```

Return that boolean in every row and aggregate:

```python
"bootstrap_tool_turn_dropped": all(
    row["bootstrap_tool_turn_dropped"] for row in rows
)
```

The fake experiment client must inspect the actual prompt received by its `complete` method and detect the expected working-summary line under `Recent working file summaries:`. It must not inspect `agent.session` or legacy builder metadata. Before the follow-up, the harness derives that line from the canonical summary already produced by the bootstrap read:

```python
summary = agent.session["memory"]["file_summaries"][task["filename"]]["summary"]
expected_working_line = f"{task['filename']} -> {summary}"
```

Pass `expected_working_line` into the fake client, then require a non-empty expected line plus both the marker and that exact line inside the `<pico:memory_index>` block in the captured prompt; a matching fact or marker after `</pico:memory_index>` does not count. Add a negative test with the marker/line outside the closed block. This preserves the existing read-file summary format (including its line-number prefix) rather than changing the memory subsystem for an experiment. Apply the canonical filler, captured bootstrap id, and `bootstrap_tool_turn_dropped` row/aggregate to both `_run_memory_variant` / `run_memory_dependency_experiment` and `_run_memory_task_variant` / `run_large_scale_memory_experiment`. Use `bool(rows) and all(row["bootstrap_tool_turn_dropped"] for row in rows)` for every aggregate, and add a zero-repetitions regression that asserts every variant reports `False` rather than vacuous success.

Update the stale private re-export in `pico/evaluation/metrics_experiments.py` to:

```python
from .experiments_synthetic import (
    _age_bootstrap_messages as _age_bootstrap_messages,
)
```

Do not retain an `_age_bootstrap_read_history` compatibility alias; importing
`pico.evaluation.metrics` in the Task 10 gate proves the rename remains valid.

- [ ] **Step 5: Remove the memory-ablation skip and assert relative behavior**

Remove `@pytest.mark.legacy_string_path`, `@pytest.mark.skip`, and the obsolete cleanup comment from `tests/test_metrics.py`. Replace hard-coded 0/1 assertions with:

```python
on = artifact["variants"]["memory_on"]
off = artifact["variants"]["memory_off"]
irrelevant = artifact["variants"]["memory_irrelevant"]
assert on["bootstrap_tool_turn_dropped"] is True
assert off["bootstrap_tool_turn_dropped"] is True
assert irrelevant["bootstrap_tool_turn_dropped"] is True
assert on["repeated_reads"] < off["repeated_reads"]
assert on["repeated_reads"] < irrelevant["repeated_reads"]
assert on["memory_hit_rate"] > off["memory_hit_rate"]
assert on["memory_hit_rate"] > irrelevant["memory_hit_rate"]
assert on["correct_rate"] == off["correct_rate"] == irrelevant["correct_rate"] == 1.0
```

Apply the same canonical filler, irrelevant recent-file, and bootstrap-drop precondition to `pico/evaluation/experiments_real.py`. Wrap the underlying real provider in a local recording proxy that preserves the provider's native/fallback capability: record `complete_v2` messages for a native client, otherwise record the flattened `complete` prompt. `FallbackAdapter` is a special fallback shim: it exposes `complete_v2` but has `supports_native_tools is False`. For that case, create a new `FallbackAdapter(_FallbackRecordingProvider(provider._inner))` so Pico still sees a v2 fallback adapter while the recorder observes the adapter's actual flattened `complete` prompt. Do not wrap an existing `FallbackAdapter` directly in `_FallbackRecordingProvider`, because it has no legacy `complete` method to delegate to. Add a regression that passes `FallbackAdapter(FakeModelClient(...))` through `_recording_provider`, calls `complete_v2`, and asserts the recorded entry is `("prompt", ...)`, not `("messages", ...)`. Capture the first actual provider call made by the follow-up `agent.ask()`, and set `bootstrap_tool_turn_dropped` only by checking the captured wire messages/prompt for the non-empty bootstrap tool-use id. A missing id or missing recorded follow-up call is a failing precondition, never a successful `True` value.

- [ ] **Step 6: Run memory correctness gates**

```bash
uv run pytest tests/test_context_sources.py tests/test_agent_loop_injection_sent.py tests/test_metrics.py tests/test_memory_quality_benchmark.py -q
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2(repetitions=1)'
```

Expected: tests pass; memory-quality reports 8/8; one-repetition ablation satisfies all relative assertions and records the bootstrap-drop precondition for every variant.

- [ ] **Step 7: Commit working-summary injection and repaired evidence**

```bash
git add pico/context/sources.py pico/evaluation/experiments_synthetic.py pico/evaluation/experiments_real.py pico/evaluation/metrics_experiments.py tests/test_context_sources.py tests/test_agent_loop_injection_sent.py tests/test_metrics.py tests/test_memory_quality_benchmark.py
git commit -m "fix(memory): inject working summaries into sent requests"
```

---

### Task 11: Commit Canonical Messages Copy-on-Write and Persist Tool Pairs Once

**Files:**

- Modify: `pico/agent_loop.py`
- Modify: `pico/task_state.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_agent_loop_digest.py`
- Modify: `tests/test_agent_loop_v2_shape.py`
- Modify: `tests/test_config_context.py`
- Modify: `tests/test_message_invariants.py`
- Modify: `tests/test_p1_smoke.py`
- Modify: `tests/test_runtime_report.py`

**Interfaces:**

- `SessionCommitError` subclasses `RuntimeError` and exposes `cause: Exception`.
- `_commit_session(agent, *, messages=(), legacy_history=()) -> None`.

`legacy_history` exists only through Task 15 so the still-v2 session stays readable. Both transcript shapes are updated in one candidate and one save; only `messages` survives v3 cutover.

- [ ] **Step 1: Add COW and pair-atomicity tests**

Add `import copy` to `tests/test_agent_loop.py`, then add:

```python
def test_tool_pair_is_written_by_one_session_save_without_orphan(tmp_path, monkeypatch):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "tu_pair", "name": "read_file", "input": {"path": "README.md"}}],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    saved_transcripts = []
    original_save = agent.session_store.save

    def spy_save(session):
        saved_transcripts.append(copy.deepcopy(session["messages"]))
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", spy_save)
    assert agent.ask("read") == "done"
    writes_with_pair = [
        messages
        for messages in saved_transcripts
        if any(
            message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            and message["content"][0].get("type") == "tool_use"
            for message in messages
        )
    ]
    assert len(writes_with_pair) >= 1
    first = writes_with_pair[0]
    tool_index = next(
        index
        for index, message in enumerate(first)
        if isinstance(message.get("content"), list)
        and message["content"][0].get("type") == "tool_use"
    )
    assert first[tool_index]["content"][0]["id"] == "tu_pair"
    assert first[tool_index + 1]["content"][0]["tool_use_id"] == "tu_pair"
    assert not any(
        messages[-1].get("role") == "assistant"
        and isinstance(messages[-1].get("content"), list)
        and messages[-1]["content"][0].get("type") == "tool_use"
        for messages in saved_transcripts
    )


def test_side_effect_then_pair_save_failure_stops_before_another_provider_call(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_write",
                "name": "write_file",
                "input": {"path": "created.txt", "content": "created\n"},
            }],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "must not be requested"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save

    def fail_pair(session):
        if any(
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
            for message in session.get("messages", [])
        ):
            raise OSError("pair save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_pair)
    with pytest.raises(OSError, match="pair save failed"):
        agent.ask("write file")
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert len(provider.calls) == 1
    assert agent.current_task_state.stop_reason == "persistence_error"
    assert agent.current_task_state.status == "failed"
    assert [
        (message["role"], message["content"])
        for message in agent.session["messages"]
    ] == [("user", "write file")]
    assert agent.current_task_state.recovery_checkpoint_id


def test_pair_save_primary_error_survives_terminal_persistence_failure(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_write",
                "name": "write_file",
                "input": {"path": "created.txt", "content": "created\n"},
            }],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save
    user_turn_saved = False

    def fail_pair_then_terminal(session):
        nonlocal user_turn_saved
        has_tool_use = any(
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
            for message in session.get("messages", [])
        )
        if has_tool_use:
            raise OSError("pair save failed")
        if user_turn_saved:
            raise RuntimeError("terminal persistence failed")
        user_turn_saved = True
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_pair_then_terminal)
    with pytest.raises(OSError, match="pair save failed"):
        agent.ask("write file")
    assert len(provider.calls) == 1
    assert all(
        not (
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
        )
        for message in agent.session["messages"]
    )
```

- [ ] **Step 2: Confirm the current code persists an orphan before execution**

```bash
uv run pytest tests/test_agent_loop.py::test_tool_pair_is_written_by_one_session_save_without_orphan tests/test_agent_loop.py::test_side_effect_then_pair_save_failure_stops_before_another_provider_call -q
```

Expected: failures because tool_use and tool_result are saved separately and pair-save failure handling is absent.

- [ ] **Step 3: Add a single private COW helper**

In `pico/agent_loop.py`:

```python
from copy import deepcopy

from .messages import append_messages, make_tool_pair


class SessionCommitError(RuntimeError):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _commit_session(agent, *, messages=(), legacy_history=()):
    safe_messages = tuple(agent.redact_artifact(message) for message in messages)
    safe_history = tuple(agent.redact_artifact(item) for item in legacy_history)
    candidate = deepcopy(agent.session)
    candidate["messages"] = append_messages(
        candidate.get("messages", []),
        *safe_messages,
    )
    if "history" in candidate:
        candidate["history"] = [
            *list(candidate.get("history", []) or []),
            *safe_history,
        ]
    try:
        saved_path = agent.session_store.save(candidate)
    except Exception as exc:
        raise SessionCommitError(exc) from exc
    agent.session = candidate
    agent.session_path = saved_path
```

No helper may append to `agent.session["messages"]` before save.

- [ ] **Step 4: Route plain user and assistant text through COW**

Replace the old append helpers with constructors that do not mutate:

```python
def _plain_message(role, text, *, origin=""):
    meta = {"created_at": now()}
    if origin:
        meta["origin"] = origin
    return {"role": role, "content": str(text), "_pico_meta": meta}
```

At turn start, call one `_commit_session` with the plain user message and its temporary v2 history mirror. Do the same for normal final/limit assistant messages. Delete all AgentLoop calls to `agent.record_message` and `agent.record`.

- [ ] **Step 5: Refactor digesting into a non-persisting preparation helper**

Rename `_append_tool_result` to `_prepare_tool_result`. Keep its digest threshold, raw artifact write, source hash, and display-content behavior, but return:

```python
return display_content, {
    "digest_applied": digest_applied,
    "source_hash": source_hash,
}
```

It must not call `agent.record_message` or save the session.

Migrate every direct helper test to the new pure/COW contract rather than
keeping a mutating compatibility wrapper:

- `tests/test_agent_loop_v2_shape.py` tests `_plain_message` and
  `make_tool_pair` shapes/metadata without a fake `record_message`.
- `tests/test_config_context.py` calls `_prepare_tool_result` and asserts its
  display content and digest metadata without inspecting persisted messages.
- `tests/test_p1_smoke.py` replaces the four append-helper imports with
  `_commit_session`, `_plain_message`, `_prepare_tool_result`, and
  `SessionCommitError`.

- [ ] **Step 6: Execute first, then build and commit the pair**

In the `ToolAction` branch:

```python
tool_use_id = action.tool_use_id or f"toolu_{uuid.uuid4().hex[:12]}"
agent.emit_trace(
    task_state,
    "tool_started",
    {"name": name, "args": args, "tool_use_id": tool_use_id},
)
tool_result = agent.execute_tool(name, args)
result = tool_result.content
metadata = dict(tool_result.metadata or {})
tool_change_id = str(metadata.get("tool_change_id", "") or "")
effect_class = str(
    metadata.get(
        "effect_class",
        "read_only" if metadata.get("read_only") else "workspace_write",
    )
)
if tool_change_id and effect_class == "workspace_write":
    run_tool_change_ids.append(tool_change_id)
display_result, digest_meta = _prepare_tool_result(
    agent,
    content=result,
    tool_name=name,
    tool_args=args,
)
pair = make_tool_pair(
    name=name,
    arguments=args,
    tool_use_id=tool_use_id,
    result_content=display_result,
    created_at=now(),
    tool_status=metadata["tool_status"],
    effect_class=effect_class,
    tool_change_id=tool_change_id,
    result_meta=digest_meta,
)
try:
    _commit_session(
        agent,
        messages=pair,
        legacy_history=({
            "role": "tool",
            "name": name,
            "args": args,
            "content": result,
            "created_at": now(),
        },),
    )
except SessionCommitError as exc:
    task_state.stop_persistence_error("Session persistence failed.")
    try:
        _finish_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=task_state.final_answer,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            completion_usage_totals=completion_usage_totals,
            trigger="persistence_error",
        )
    except Exception:
        logger.warning("best-effort persistence-error finalization failed", exc_info=True)
    raise exc.cause
```

The pair-save exception is always the primary error. The call to the current
serial finalizer is best effort in this Task 11 path: a second persistence
failure must never mask `exc.cause` or resume the loop, but it may stop later
terminal artifacts. Task 13 later centralizes independent terminal
finalization.

Only after successful pair persistence should the loop update step counters, task state, tool-finished trace, resume checkpoint, and continue.

- [ ] **Step 7: Add the persistence TaskState transition**

In `pico/task_state.py`:

```python
def stop_persistence_error(self, final_answer=""):
    return self.stop(
        STOP_REASON_PERSISTENCE_ERROR,
        status=STATUS_FAILED,
        final_answer=final_answer,
    )
```

- [ ] **Step 8: Run pairing, digest, and persistence tests**

```bash
uv run pytest tests/test_agent_loop.py tests/test_agent_loop_digest.py tests/test_agent_loop_v2_shape.py tests/test_config_context.py tests/test_message_invariants.py tests/test_p1_smoke.py tests/test_runtime_report.py -q
```

Expected: all pass; no saved transcript ends on a tool_use; the side-effect failure test makes only one Provider call.

- [ ] **Step 9: Commit COW pairing**

```bash
git add pico/agent_loop.py pico/task_state.py tests/test_agent_loop.py tests/test_agent_loop_digest.py tests/test_agent_loop_v2_shape.py tests/test_config_context.py tests/test_message_invariants.py tests/test_p1_smoke.py tests/test_runtime_report.py
git commit -m "fix(runtime): persist tool messages as one pair"
```

---

### Task 12: Make Tool Status, Effects, and Read-Only Enforcement Truthful

**Files:**

- Modify: `pico/tool_executor.py`
- Modify: `pico/tools.py`
- Modify: `pico/memory/tools.py`
- Modify: `pico/repo_map.py`
- Modify: `pico/agent_loop.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/memory/test_memory_tools.py`
- Modify: `tests/test_memory_save_topic.py`
- Modify: `tests/test_safety_invariants.py`
- Modify: `tests/test_recovery_e2e.py`

**Interfaces:**

- Closed statuses: `ok | rejected | error | partial_success`.
- Closed effect classes: `read_only | memory_write | workspace_write`.
- Every `ToolExecutionResult.metadata` contains `tool_status`, `effect_class`, and `read_only`.
- Only `workspace_write` creates snapshots/file entries and joins a recoverable turn checkpoint.
- `memory_write` creates a non-restorable Tool Change audit record but no workspace recovery checkpoint.

**Boundary clarification (implementation review, 2026-07-10):**

- Resolve an effect class as soon as the registry lookup is available, before
  any approval decision. Every return from `ToolExecutor.execute()` — including
  allowed-tools rejection, unknown tool, validation/repeat rejection and
  command/approval rejection — must pass that class to `_metadata`.
- A truly unknown tool has no trustworthy registry risk flag, so preserve the
  current fail-safe `read_only=False` contract by assigning it
  `effect_class="workspace_write"`. The `_effect_class(name, risky)` fallback
  remains `read_only` for a future *registered* unknown-safe tool and
  `workspace_write` for a registered risky one.
- `tests/test_tool_executor.py::build_agent` must accept `outputs=None, **kwargs`
  so the memory-write end-to-end test can construct its scripted/read-only
  agents. Migrate existing direct-runner tests: unavailable/not-found/I/O/topic
  failures now raise from the runner and become executor `error` results;
  `delegate` is read-only and no longer receives a Tool Change record.

- [ ] **Step 1: Add effect and status regression tests**

Add focused cases to `tests/test_tool_executor.py`:

```python
def test_effect_class_table_is_explicit():
    assert _effect_class("read_file", False) == "read_only"
    assert _effect_class("delegate", False) == "read_only"
    assert _effect_class("memory_save", False) == "memory_write"
    assert _effect_class("write_file", True) == "workspace_write"
    assert _effect_class("unknown_safe", False) == "read_only"
    assert _effect_class("unknown_risky", True) == "workspace_write"


def test_read_only_agent_rejects_memory_write_before_runner(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, read_only=True)
    runner = Mock(return_value="must not run")
    agent.tools["memory_save"]["run"] = runner
    result = agent.execute_tool("memory_save", {"note": "remember this"})
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["effect_class"] == "memory_write"
    assert result.metadata["read_only"] is False
    assert result.metadata["tool_error_code"] == "read_only_block"
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", {"path": "README.md"}),
        ("delegate", {"task": "inspect README", "max_steps": 1}),
    ],
)
def test_read_only_agent_allows_read_effects(tmp_path, name, arguments):
    agent = build_agent(tmp_path, read_only=True)
    agent.tools[name]["run"] = Mock(return_value="ok")
    result = agent.execute_tool(name, arguments)
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["effect_class"] == "read_only"
    assert result.metadata["read_only"] is True


def test_memory_write_is_audited_without_workspace_snapshot_or_recovery_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        outputs=[
            '<tool>{"name":"memory_save","args":{"note":"remember this"}}</tool>',
            "<final>done</final>",
        ],
    )
    assert agent.ask("remember this") == "done"
    events = read_trace(agent)
    memory_event = next(
        event
        for event in events
        if event.get("event") == "tool_executed"
        and event.get("name") == "memory_save"
    )
    assert memory_event["tool_status"] == "ok"
    assert memory_event["effect_class"] == "memory_write"
    assert memory_event["read_only"] is False
    assert memory_event["tool_change_id"]
    assert memory_event["affected_paths"] == []
    assert agent.current_task_state.recovery_checkpoint_id == ""


def test_runner_keyboard_interrupt_finalizes_pending_change_then_reraises(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    agent.tools["write_file"]["run"] = lambda args: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "x.txt", "content": "x"})
    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["status"] == "interrupted"


def test_post_runner_interrupt_closes_workspace_change_then_reraises(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    original_capture = agent.workspace_observer.capture
    capture_calls = 0

    def interrupt_after_runner():
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise KeyboardInterrupt()
        return original_capture()

    agent.tools["run_shell"]["run"] = lambda args: "exit_code: 0\nstdout:\nok\nstderr:\n(empty)"
    monkeypatch.setattr(agent.workspace_observer, "capture", interrupt_after_runner)
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("run_shell", {"command": "printf ok", "timeout": 5})
    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["status"] == "interrupted"


def test_post_runner_interrupt_closes_memory_audit_then_reraises(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.tools["memory_save"]["run"] = lambda args: "saved"
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("memory_save", {"note": "remember this"})
    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["effect_class"] == "memory_write"
    assert records[-1]["status"] == "interrupted"
```

Add memory runner tests that missing store/retrieval, missing memory file, I/O error, and invalid topic do not produce `tool_status == "ok"`. Add a protected `.pico/memory/notes/**` write test that expects `rejected` before the runner.

Also add a table-driven executor test that exercises an unknown tool and at
least one known validation/approval rejection, asserting all result metadata
contains a closed `effect_class` and derives `read_only` exclusively from it.

- [ ] **Step 2: Confirm current false-ok/read-only behavior**

```bash
uv run pytest tests/test_tool_executor.py tests/memory/test_memory_tools.py tests/test_memory_save_topic.py tests/test_tools.py -q
```

Expected: failures because `memory_save` is read-only, several runners return error strings, and Ctrl-C leaves pending Tool Change state.

- [ ] **Step 3: Correct the effect table and metadata constructor**

In `pico/tool_executor.py`:

```python
_EFFECT_CLASS_BY_TOOL = {
    "read_file": "read_only",
    "list_files": "read_only",
    "search": "read_only",
    "delegate": "read_only",
    "memory_list": "read_only",
    "memory_read": "read_only",
    "memory_search": "read_only",
    "repo_lookup": "read_only",
    "memory_save": "memory_write",
    "run_shell": "workspace_write",
    "write_file": "workspace_write",
    "patch_file": "workspace_write",
}


def _effect_class(name, risky):
    return _EFFECT_CLASS_BY_TOOL.get(
        name,
        "workspace_write" if risky else "read_only",
    )
```

Make `effect_class` a required `_metadata` argument and derive:

```python
"effect_class": effect_class,
"read_only": effect_class == "read_only",
```

Update every `_metadata` and `_finalize_tool_side_effects` call to pass the already-computed effect class. Never derive read-only from `tool["risky"]`.

For the unknown-tool branch, set `effect_class="workspace_write"` explicitly
before calling `_metadata`; this is deliberately more conservative than the
generic `_effect_class(name, False)` fallback.

- [ ] **Step 4: Enforce agent read-only before approval and runner execution**

Immediately after resolving the tool/effect class:

```python
if agent.read_only and effect_class != "read_only":
    return ToolExecutionResult(
        content=f"error: read-only mode blocks {name}",
        metadata=_metadata(
            "rejected",
            effect_class=effect_class,
            tool_error_code="read_only_block",
            security_event_type="read_only_block",
            risk_level="high",
        ),
    )
```

Keep `delegate` allowed: its own `spawn_delegate` constructs a read-only child.

- [ ] **Step 5: Separate audit records from recoverable workspace records**

Use:

```python
records_tool_change = effect_class in {"memory_write", "workspace_write"}
records_recovery = effect_class == "workspace_write"
```

- Start a pending Tool Change when `records_tool_change`.
- Capture path snapshots, observer state, affected paths, file entries, and shell deltas only when `records_recovery`.
- Finalize `memory_write` with empty affected paths/file entries.
- In `AgentLoop` append `tool_change_id` to `run_tool_change_ids` only when `effect_class == "workspace_write"`.
- Since every executor result now has an effect class, remove the old
  AgentLoop `read_only`-derived fallback and consume `metadata["effect_class"]`
  directly.

- [ ] **Step 6: Move predictable failures into validation**

In `pico/tools.validate_tool`:

- For `write_file`/`patch_file`, call the protected-note path check and raise `ValueError` if it returns a refusal.
- For `memory_save`, keep note length/scope checks and add:

```python
topic = str(args.get("topic", "")).strip()
if topic and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", topic):
    raise ValueError("invalid topic")
note_type = str(args.get("type", "feedback")).strip()
if not note_type:
    raise ValueError("type must not be empty")
```

Remove protected-path refusal returns from `tool_write_file` and `tool_patch_file`. The runner should perform the write or raise.

- [ ] **Step 7: Make runners return only domain success**

In `pico/memory/tools.py`:

```python
if store is None:
    raise RuntimeError("memory_store unavailable")
```

and:

```python
if retrieval is None:
    raise RuntimeError("memory_retrieval unavailable")
```

Remove catches that convert `FileNotFoundError`, `OSError`, or store `ValueError` to strings; prevalidated arguments call the store directly and runtime failures propagate to `ToolExecutor`.

In `pico/repo_map.py`, raise `RuntimeError("repo_map unavailable")` instead of returning it. Symbol validation remains in `validate_tool`.

Only `ToolExecutor` formats model-visible failures using `error: tool {name} failed: {exc}`.

- [ ] **Step 8: Preserve Ctrl-C as Ctrl-C and close the current Tool Change**

After a pending Tool Change has been created, cover the whole remaining
execution lifecycle — pre/post snapshots, observer work, the runner, memory
updates and side-effect finalization — with a dedicated `KeyboardInterrupt`
handler. It must never be limited to `tool["run"](args)`, because an interrupt
after a successful runner can otherwise leave the pending record unclosed.

Use a small helper that only finalizes a record which is still pending:

```python
def _finalize_interrupted_pending(agent, pending_record):
    if pending_record is None:
        return
    try:
        current = agent.checkpoint_store.load_tool_change_record(
            pending_record["tool_change_id"]
        )
        if current.get("status") == "pending":
            agent.tool_change_recorder.finalize(
                pending_record["tool_change_id"],
                status="interrupted",
            )
    except Exception:
        pass
```


Place the existing snapshot setup, runner call, post-run observer/snapshot
work, `agent.update_memory_after_tool`, `_finalize_tool_side_effects`,
and its current ordinary `except Exception` body under this same outer
`try`. Insert this dedicated branch immediately before the ordinary exception
branch:

```python
except KeyboardInterrupt:
    _finalize_interrupted_pending(agent, pending_record)
    raise
```

Initialize snapshot/observer locals before this `try` so the existing
ordinary-error branch can still safely decide whether a workspace change was
partial. The primary `KeyboardInterrupt` must always be re-raised.

- [ ] **Step 9: Run tool/recovery integration**

```bash
uv run pytest tests/test_tool_executor.py tests/test_tools.py tests/memory/test_memory_tools.py tests/test_memory_save_topic.py tests/test_safety_invariants.py tests/test_recovery_e2e.py -q
```

Expected: all pass; no predictable error string is reported as ok; memory_write has audit but no recoverable checkpoint.

- [ ] **Step 10: Commit structured tool semantics**

```bash
git add pico/tool_executor.py pico/tools.py pico/memory/tools.py pico/repo_map.py pico/agent_loop.py tests/test_tool_executor.py tests/test_tools.py tests/memory/test_memory_tools.py tests/test_memory_save_topic.py tests/test_safety_invariants.py tests/test_recovery_e2e.py
git commit -m "fix(tools): enforce truthful status and effects"
```

---

### Task 13: Close Every Started Run Through One Best-Effort Terminal Finalizer

**Files:**

- Modify: `pico/agent_loop.py`
- Modify: `pico/task_state.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_runtime_report.py`
- Modify: `tests/test_task_state.py`

**Interfaces:**

`_finalize_run` consumes agent, TaskState, user/final text, run timing, Tool Change ids, verification evidence, completion totals, trigger, optional terminal message, and optional primary exception; it returns the final text or raises an unmasked primary/finalization exception.

TaskState adds `interrupted` and `runtime_error`. Provider errors of any `Exception` remain `model_error`. Persistence remains `persistence_error`. `KeyboardInterrupt` is never converted to a tool error.

**Boundary clarifications (implementation review, 2026-07-10):**

- Import `Mock` in `tests/test_agent_loop.py` and migrate the existing
  `test_terminal_paths_share_finish_run_helper` to spy on `_finalize_run`.
- The initial plain-user COW commit remains outside the started-run boundary:
  its failure re-raises the original save error without creating a TaskState,
  run directory, report, or runtime-terminal message. After `TaskState.create`
  and assignment to `agent.current_task_state`, the outer boundary starts
  *before* `RunStore.start_run` and the `run_started` trace so failures in
  either are terminalized as `runtime_error` whenever their stores remain
  writable.
- Replace the Task 11 pair-save inner handler with propagation of its
  `SessionCommitError` to the outer handler. The outer handler is the only
  place that marks `persistence_error`, invokes `_finalize_run`, and re-raises
  `exc.cause`; preserve the already-collected workspace `tool_change_id`.
- In `_create_resume_checkpoint`, wrap the `OSError` from
  `agent.create_checkpoint(...)` in `SessionCommitError`. This makes an
  in-run checkpoint session-save failure reach the persistence handler rather
  than the generic runtime handler. Leave other exceptions as runtime errors;
  the helper's only actual session save is currently in
  `pico/checkpoint.py::create_checkpoint`.
- Split report construction and report writing into separate finalizer
  attempts. The report produced before a successful write includes all errors
  known so far as `finalization_errors`; a later write failure is represented
  by a second best-effort `finalization_failed` trace event when trace storage
  is available.
- `pico/runtime.py::build_report()` remains a pure dictionary builder; add the
  finalizer-only `finalization_errors` field in `pico/agent_loop.py` rather
  than changing the runtime-wide report interface. Existing ToolExecutor
  interrupt tests stay in Task 12; Task 13's run-level interrupt coverage
  belongs in `tests/test_agent_loop.py`.

- [ ] **Step 1: Add terminal-path tests**

Add to `tests/test_agent_loop.py`:

```python
class RaisingProvider:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, error):
        self.error = error

    def complete_v2(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        raise self.error


def read_trace(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(
            agent.current_task_state
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize("error", [ValueError("provider bad"), OSError("provider io")])
def test_any_provider_exception_closes_model_error_and_reraises_original(
    tmp_path,
    error,
):
    provider = RaisingProvider(error)
    agent = build_native_agent(tmp_path, provider)
    with pytest.raises(type(error), match=str(error)):
        agent.ask("fail")
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "model_error"
    assert agent.run_store.report_path(agent.current_task_state).exists()
    terminal = agent.session["messages"][-1]
    assert terminal["role"] == "assistant"
    assert terminal["_pico_meta"]["origin"] == "runtime_terminal"
    assert str(error) not in terminal["content"]


def test_preflight_exception_becomes_runtime_error(tmp_path, monkeypatch):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    monkeypatch.setattr(agent, "refresh_prefix", Mock(side_effect=ValueError("preflight")))
    with pytest.raises(ValueError, match="preflight"):
        agent.ask("start")
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "runtime_error"
    assert agent.run_store.report_path(agent.current_task_state).exists()


def test_keyboard_interrupt_closes_run_and_reraises(tmp_path):
    agent = build_native_agent(
        tmp_path,
        RaisingProvider(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        agent.ask("interrupt")
    assert agent.current_task_state.status == "stopped"
    assert agent.current_task_state.stop_reason == "interrupted"
    assert agent.run_store.report_path(agent.current_task_state).exists()
    assert agent.session["messages"][-1]["_pico_meta"]["origin"] == "runtime_terminal"


def test_finalizer_failure_does_not_mask_provider_exception(tmp_path, monkeypatch):
    primary = ValueError("primary provider failure")
    agent = build_native_agent(tmp_path, RaisingProvider(primary))
    monkeypatch.setattr(
        agent.run_store,
        "write_report",
        Mock(side_effect=OSError("report unavailable")),
    )
    with pytest.raises(ValueError, match="primary provider failure"):
        agent.ask("fail")
    assert agent.current_task_state.stop_reason == "model_error"
    events = read_trace(agent)
    assert any(event["event"] == "run_finished" for event in events)
    failure = next(
        event for event in events if event["event"] == "finalization_failed"
    )
    assert "report unavailable" in " ".join(failure["finalization_errors"])


def test_finalizer_failure_without_primary_is_raised(tmp_path, monkeypatch):
    agent = build_native_agent(
        tmp_path,
        NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            ),
        ]),
    )
    monkeypatch.setattr(
        agent.run_store,
        "write_report",
        Mock(side_effect=OSError("report unavailable")),
    )
    with pytest.raises(OSError, match="report unavailable"):
        agent.ask("finish")


def test_final_message_save_failure_is_persistence_error(tmp_path, monkeypatch):
    agent = build_native_agent(
        tmp_path,
        NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            ),
        ]),
    )
    original_save = agent.session_store.save

    def fail_assistant(session):
        if (
            session.get("messages")
            and session["messages"][-1].get("role") == "assistant"
        ):
            raise OSError("assistant save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_assistant)
    with pytest.raises(OSError, match="assistant save failed"):
        agent.ask("finish")
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "persistence_error"


def test_initial_user_save_failure_does_not_start_a_run(tmp_path, monkeypatch):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    monkeypatch.setattr(
        agent.session_store,
        "save",
        Mock(side_effect=OSError("user save failed")),
    )
    with pytest.raises(OSError, match="user save failed"):
        agent.ask("start")
    assert agent.current_task_state is None
    assert agent.session["messages"] == []
    assert not list((tmp_path / ".pico" / "runs").glob("run_*"))


@pytest.mark.parametrize("failure", ["start_run", "run_started"])
def test_run_start_artifact_failure_is_runtime_terminal(tmp_path, monkeypatch, failure):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    if failure == "start_run":
        monkeypatch.setattr(agent.run_store, "start_run", Mock(side_effect=OSError("run start failed")))
        expected = "run start failed"
    else:
        original_emit_trace = agent.emit_trace

        def fail_run_started(task_state, event, payload=None):
            if event == "run_started":
                raise OSError("run trace failed")
            return original_emit_trace(task_state, event, payload)

        monkeypatch.setattr(agent, "emit_trace", fail_run_started)
        expected = "run trace failed"
    with pytest.raises(OSError, match=expected):
        agent.ask("start")
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "runtime_error"


def test_in_run_checkpoint_session_save_failure_is_persistence_error(tmp_path, monkeypatch):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_checkpoint",
                "name": "write_file",
                "input": {"path": "note.txt", "content": "saved\\n"},
            }],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save
    saves = 0

    def fail_checkpoint_save(session):
        nonlocal saves
        saves += 1
        if saves == 3:
            raise OSError("checkpoint save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_checkpoint_save)
    with pytest.raises(OSError, match="checkpoint save failed"):
        agent.ask("write")
    assert len(provider.calls) == 1
    assert agent.current_task_state.stop_reason == "persistence_error"
    assert agent.current_task_state.status == "failed"
```

Add a provider-error report-build regression by monkeypatching
`agent.build_report` to raise `OSError("report build unavailable")`; it must
re-raise the original provider exception, retain `model_error`, and emit a
bounded `finalization_failed` trace when trace storage is writable. Add a
normal successful-final report assertion that `finalization_errors == []`.

Migrate the Task 11 pair-save failure assertion in
`test_side_effect_then_pair_save_failure_stops_before_another_provider_call`:
the canonical transcript must still contain no tool-use block or orphan, begin
with the original user text, and end with an assistant message whose
`_pico_meta.origin` is `runtime_terminal`. Its content must be the generic
persistence terminal text, not the raw save error. Keep the one-provider-call
and recovery-evidence assertions.

- [ ] **Step 2: Confirm current exception coverage is incomplete**

```bash
uv run pytest tests/test_agent_loop.py -k 'provider_exception or preflight_exception or keyboard_interrupt or finalizer_failure' -q
```

Expected: failures for non-`RuntimeError` Provider exceptions, running TaskState after preflight errors, missing runtime terminal messages, and masked finalizer errors.

- [ ] **Step 3: Add stable TaskState transitions**

In `pico/task_state.py`:

```python
STOP_REASON_INTERRUPTED = "interrupted"
STOP_REASON_RUNTIME_ERROR = "runtime_error"


def stop_interrupted(self, final_answer=""):
    return self.stop(
        STOP_REASON_INTERRUPTED,
        status=STATUS_STOPPED,
        final_answer=final_answer,
    )


def stop_runtime_error(self, final_answer=""):
    return self.stop(
        STOP_REASON_RUNTIME_ERROR,
        status=STATUS_FAILED,
        final_answer=final_answer,
    )
```

Add exact serialization tests in `tests/test_task_state.py`.

- [ ] **Step 4: Add generic runtime terminal messages**

In `pico/agent_loop.py`:

```python
_RUNTIME_TERMINAL_TEXT = {
    "model_error": "The model request failed. This turn was stopped.",
    "interrupted": "This turn was interrupted before completion.",
    "persistence_error": "This turn stopped because session state could not be saved.",
    "runtime_error": "This turn stopped because the runtime failed.",
}


def _runtime_terminal_message(stop_reason):
    return _plain_message(
        "assistant",
        _RUNTIME_TERMINAL_TEXT[stop_reason],
        origin="runtime_terminal",
    )
```

Do not include the exception string, stack trace, Provider body, or raw model response.

- [ ] **Step 5: Implement independent best-effort finalizer steps**

Replace `_finish_run` with `_finalize_run`.

The finalizer must:

1. Assume TaskState is already terminal before entry.
2. Best-effort COW-commit `terminal_message` when non-empty.
3. Independently attempt task-state write.
4. Independently attempt resume checkpoint creation.
5. Independently attempt recovery checkpoint finalization.
6. Attempt recovery-created trace and verification evidence if a recovery record exists.
7. Emit `run_finished` with current status, stop reason, duration, and the errors accumulated so far.
8. Build a report with completion totals, add `finalization_errors`, and independently write it.
9. If `primary_exception is None` and any finalization error exists, raise the first stored exception object; otherwise return `final`.

Store both bounded strings and exception objects internally:

```python
finalization_errors = []
finalization_exceptions = []


def attempt(label, operation):
    try:
        return operation()
    except Exception as exc:
        stored_exception = exc.cause if isinstance(exc, SessionCommitError) else exc
        finalization_exceptions.append(stored_exception)
        finalization_errors.append(
            f"{label}: {type(stored_exception).__name__}: {stored_exception}"[:300]
        )
        logger.exception("run finalization step failed: %s", label)
        return None
```

Call `attempt("report_build", ...)` for `agent.build_report(...)`, add a copy
of the current bounded `finalization_errors` to the returned dictionary, then
call `attempt("report_write", ...)` only when the report build succeeded. If
`run_finished` was emitted before a later report build/write failure, append a
second bounded `finalization_failed` trace only when trace storage is still
writable; never rewrite the earlier event or mask a primary exception.

- [ ] **Step 6: Put the post-run-start lifecycle inside one outer boundary**

Import `STATUS_RUNNING` and `STOP_REASON_PERSISTENCE_ERROR` from `pico.task_state`.
After the initial user COW persistence succeeds, create and assign `TaskState`,
initialize the per-run accumulator lists, then put `RunStore.start_run`, the
`run_started` trace, preflight, attempt loop, and normal return body inside one
outer `try`. The initial user COW stays before this boundary. Add these exact
outer handlers:

```python
except KeyboardInterrupt as exc:
    if task_state.status == STATUS_RUNNING:
        task_state.stop_interrupted(_RUNTIME_TERMINAL_TEXT["interrupted"])
        _finalize_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=task_state.final_answer,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            completion_usage_totals=completion_usage_totals,
            trigger="interrupted",
            terminal_message=_runtime_terminal_message("interrupted"),
            primary_exception=exc,
        )
    raise
except SessionCommitError as exc:
    if task_state.stop_reason != STOP_REASON_PERSISTENCE_ERROR:
        task_state.stop_persistence_error(
            _RUNTIME_TERMINAL_TEXT["persistence_error"]
        )
        _finalize_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=task_state.final_answer,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            completion_usage_totals=completion_usage_totals,
            trigger="persistence_error",
            terminal_message=_runtime_terminal_message(
                "persistence_error"
            ),
            primary_exception=exc.cause,
        )
    raise exc.cause
except Exception as exc:
    if task_state.status == STATUS_RUNNING:
        task_state.stop_runtime_error(_RUNTIME_TERMINAL_TEXT["runtime_error"])
        _finalize_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=task_state.final_answer,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            completion_usage_totals=completion_usage_totals,
            trigger="runtime_error",
            terminal_message=_runtime_terminal_message("runtime_error"),
            primary_exception=exc,
        )
    raise
```

Around only `Provider.complete_v2`:

```python
try:
    raw_response = agent.model_client.complete_v2(
        system=request["system"],
        tools=request["tools"],
        messages=request["messages"],
        max_tokens=agent.max_new_tokens,
        cache_breakpoints=request["cache_control_breakpoints"],
    )
except KeyboardInterrupt:
    raise
except Exception as exc:
    task_state.stop_model_error(_RUNTIME_TERMINAL_TEXT["model_error"])
    _finalize_run(
        agent=agent,
        task_state=task_state,
        user_message=user_message,
        final=task_state.final_answer,
        run_started_at=run_started_at,
        run_tool_change_ids=run_tool_change_ids,
        run_verification_evidence=run_verification_evidence,
        completion_usage_totals=completion_usage_totals,
        trigger="model_error",
        terminal_message=_runtime_terminal_message("model_error"),
        primary_exception=exc,
    )
    raise
```

For the Task 11 pair-save branch, remove its local `try/except
SessionCommitError` block entirely. Let `_commit_session(...)` propagate to
the outer `except SessionCommitError`, which supplies the persistence runtime
terminal message, uses `exc.cause` as the primary exception, and finalizes
exactly once. Do not move the already-collected workspace `tool_change_id`
below the pair commit.

In `_create_resume_checkpoint`, use this narrow transport wrapper around the
existing `agent.create_checkpoint(...)` call:

```python
try:
    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
except OSError as exc:
    raise SessionCommitError(exc) from exc
```

Keep the subsequent task-state write and checkpoint trace outside that
wrapper; their failures remain ordinary runtime/finalization errors.

- [ ] **Step 7: Run all terminal and tool-interrupt paths**

```bash
uv run pytest tests/test_agent_loop.py tests/test_runtime_report.py tests/test_task_state.py -q
```

Expected: all pass; primary exceptions retain their original type/message; terminal reports exist whenever their stores are writable.

- [ ] **Step 8: Run the Phase 5 gate**

```bash
./scripts/check.sh
```

Expected: Ruff and all tests pass; memory-ablation skip count is now zero.

- [ ] **Step 9: Commit terminal lifecycle closure**

```bash
git add pico/agent_loop.py pico/task_state.py tests/test_agent_loop.py tests/test_runtime_report.py tests/test_task_state.py
git commit -m "fix(runtime): close every started run terminally"
```

---

### Task 14: Activate Locked, Backed-Up, Atomic Session v3 Migration

**Files:**

- Modify: `pico/session_store.py`
- Modify: `tests/test_session_store.py`
- Modify: `tests/test_session_store_migrator.py`
- Modify: `tests/test_file_lock.py`

**Interfaces:**

- `SessionStore.save(session) -> Path` keeps its public signature.
- `SessionStore.load(session_id) -> dict` returns validated v3 or raises `SessionMigrationError` before a run starts.
- Public methods acquire one store lock; `_atomic_write_locked` and `_write_backup_locked` require the caller to already hold it.

- [ ] **Step 1: Add transaction, backup, and idempotence tests**

Add `from pathlib import Path` to `tests/test_session_store_migrator.py` because the new replace-failure test uses it, then add:

```python
def test_load_migrates_v2_to_v3_and_backup_is_original_bytes(store):
    path = store.path_for("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
        "history": [{"role": "user", "content": "q"}],
    }).encode("utf-8")
    path.write_bytes(original)
    loaded = store.load("s2")
    assert loaded["schema_version"] == 3
    assert "history" not in loaded
    backups = list((path.parent / "backup").glob("s2.v2.*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_v3_load_is_idempotent_without_write_or_backup(store):
    session = {
        "id": "s3",
        "schema_version": 3,
        "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
    }
    path = store.path_for("s3")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session), encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert store.load("s3") == session
    assert store.load("s3") == session
    assert path.stat().st_mtime_ns == before
    backup_dir = path.parent / "backup"
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


def test_replace_failure_preserves_original_and_may_leave_backup(
    store,
    monkeypatch,
):
    path = store.path_for("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
    }).encode("utf-8")
    path.write_bytes(original)
    original_replace = Path.replace

    def fail_target_replace(self, target):
        if Path(target) == path:
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_target_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.load("s2")
    assert path.read_bytes() == original
    backups = list((path.parent / "backup").glob("s2.v2.*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_migration_error_preserves_original_session_bytes(store):
    path = store.path_for("bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "bad",
        "schema_version": 1,
        "history": [{"role": "runtime", "content": "invalid"}],
    }).encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(SessionMigrationError, match="unknown history role"):
        store.load("bad")
    assert path.read_bytes() == original


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", ".", "..", "x\\y"])
def test_session_id_must_be_basename_safe(store, session_id):
    with pytest.raises(ValueError, match="invalid session id"):
        store.path_for(session_id)
```

Add a lock spy test showing `load` enters `locked_file` once across read/migrate/backup/replace and never calls public `save` from inside migration.

- [ ] **Step 2: Confirm current migration is unlocked and non-atomic**

```bash
uv run pytest tests/test_session_store.py tests/test_session_store_migrator.py tests/test_file_lock.py -q
```

Expected: failures for schema 3, idempotence, safe ids, same-lock behavior, and original-file preservation.

- [ ] **Step 3: Add basename-safe path validation**

In `pico/session_store.py`:

```python
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _session_id(value):
    session_id = str(value or "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id
```

Use `_session_id` in `path`, `path_for`, `save`, and `load`. Verify a loaded file's `session["id"]` equals the requested id.

- [ ] **Step 4: Add already-locked atomic writer and unique backup writer**

```python
def _atomic_write_locked(path, payload):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_backup_locked(session_path, raw_bytes, session_id, source_version):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        f"{session_id}.v{source_version}.{time.time_ns()}."
        f"{uuid.uuid4().hex}.json"
    )
    with backup_path.open("xb") as handle:
        handle.write(raw_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    return backup_path
```

Import `os` and `re`. Delete the old `_write_backup` and all direct migration `write_text` calls.

- [ ] **Step 5: Make save and load share one lock and the internal writer**

Use:

```python
def save(self, session):
    session_id = _session_id(session["id"])
    path = self.path(session_id)
    payload = self._redactor(session)
    if int(payload.get("schema_version", 1) or 1) == 3:
        try:
            validate_messages(payload.get("messages"), require_meta=True)
        except MessageValidationError as exc:
            raise SessionMigrationError(str(exc)) from exc
    with file_lock.locked_file(self.lock_path):
        _atomic_write_locked(path, payload)
    return path


def load(self, session_id):
    session_id = _session_id(session_id)
    path = self.path(session_id)
    with file_lock.locked_file(self.lock_path):
        raw = path.read_bytes()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionMigrationError(
                f"failed to decode session {session_id}"
            ) from exc
        if not isinstance(decoded, dict) or decoded.get("id") != session_id:
            raise SessionMigrationError("session id does not match file name")
        try:
            version = int(decoded.get("schema_version", 1) or 1)
        except (TypeError, ValueError) as exc:
            raise SessionMigrationError(
                "invalid session schema version"
            ) from exc
        migrated = migrate_session_to_v3(decoded)
        if version == 3:
            return migrated
        _write_backup_locked(path, raw, session_id, version)
        payload = self._redactor(migrated)
        _atomic_write_locked(path, payload)
        return payload
```

The `load` lock covers read, decode, validate/migrate, backup, and replace. It does not call `save` and therefore cannot nest the lock.

- [ ] **Step 6: Replace old v1→v2 tests with v1/v2→v3 tests**

Delete assertions against `_migrate_v1_to_v2` and the `.v1.<timestamp>.json` exact old filename. Preserve tests for created_at and matching generated IDs through `migrate_session_to_v3` and the new `.v1.*.json`/`.v2.*.json` patterns.

- [ ] **Step 7: Run the SessionStore transaction gate**

```bash
uv run pytest tests/test_session_store.py tests/test_session_store_migrator.py tests/test_file_lock.py -q
```

Expected: all pass; repeated v3 load does not change mtime or create a backup.

- [ ] **Step 8: Commit locked v3 migration**

```bash
git add pico/session_store.py tests/test_session_store.py tests/test_session_store_migrator.py tests/test_file_lock.py
git commit -m "feat(session): activate atomic v3 migration"
```

---

### Task 15: Cut Runtime Over to v3 and Delete Every Legacy Transcript Path

**Files:**

- Modify: `pico/runtime.py`
- Modify: `pico/agent_loop.py`
- Modify: `pico/messages.py`
- Modify: `pico/cli_commands.py`
- Modify: `pico/evaluation/experiments_real.py`
- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `pico/evaluation/experiments_recovery.py`
- Modify: `pico/evaluation/fixed_benchmark.py`
- Modify: `pico/evaluation/metrics_reports.py`
- Modify: `pico/evaluation/provider_benchmark.py`
- Modify: `benchmarks/perf/bench_build_v2.py`
- Modify: `pyproject.toml`
- Modify: `tests/e2e/test_full_turn_roundtrip.py`
- Modify: `tests/test_pico.py`
- Modify: `tests/test_agent_loop_v2_shape.py`
- Modify: `tests/test_agent_loop_digest.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_config_context.py`
- Modify: `tests/test_run_store.py`
- Modify: `tests/test_runtime_report.py`
- Modify: `tests/test_safety_invariants.py`
- Modify: `tests/memory/test_runtime_wiring.py`
- Modify: `tests/test_clean_up.py`
- Delete: `pico/model_output_parser.py`
- Delete: `pico/providers/message_utils.py`
- Delete: `tests/test_model_output_parser.py`
- Delete: `tests/test_provider_message_utils.py`

**Interfaces:**

- New `Pico` sessions start with `schema_version == 3` and no `history` key.
- `Pico.reset()` is copy-on-write and keeps the session id, checkpoint items, disk artifacts, and user memory files.
- `Pico.last_request_metadata` replaces `last_prompt_metadata`.
- `Pico.record`, `Pico.record_message`, `Pico.history_text`, `Pico.prompt`, parser delegates, and runtime `last_completion_metadata` are removed.

- [ ] **Step 1: Add v3 runtime, repeated-tool, and reset tests**

Add to `tests/test_pico.py`:

```python
def test_new_runtime_persists_v3_messages_only(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    assert agent.ask("q") == "done"
    persisted = json.loads(Path(agent.session_path).read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 3
    assert "history" not in persisted
    validate_messages(persisted["messages"], require_meta=True)


def test_repeated_tool_detection_reads_canonical_tool_use_blocks(tmp_path):
    agent = build_agent(tmp_path, [])
    pairs = []
    for index, path in enumerate(("a.py", "b.py", "a.py", "b.py")):
        pairs.extend(make_tool_pair(
            name="read_file",
            arguments={"path": path},
            tool_use_id=f"tu_{index}",
            result_content="body",
            created_at="t",
            tool_status="ok",
            effect_class="read_only",
        ))
    agent.session["messages"].extend(pairs)
    assert agent.repeated_tool_call("read_file", {"path": "a.py"}) is True
    assert agent.repeated_tool_call("read_file", {"path": "c.py"}) is False


def test_reset_clears_transient_v3_state_and_preserves_audit_items(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.ask("q")
    session_id = agent.session["id"]
    agent.session["recently_recalled"] = ["note"]
    agent.session["_recall_errors"] = {"count": 2, "last": "x"}
    agent.session["working_memory"] = {"task_summary": "goal", "recent_files": ["a.py"]}
    agent.session["memory"] = {"file_summaries": {"a.py": {"summary": "fact"}}}
    agent.session["checkpoints"] = {
        "current_id": "c1",
        "items": {"c1": {"checkpoint_id": "c1"}},
    }
    agent.session["resume_state"] = {"status": "full-valid"}
    agent.session["recovery"] = {"current_checkpoint_id": "r1"}
    agent.reset()
    assert agent.session["id"] == session_id
    assert agent.session["messages"] == []
    assert agent.session["recently_recalled"] == []
    assert "_recall_errors" not in agent.session
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}
    assert agent.session["checkpoints"]["current_id"] == ""
    assert agent.session["checkpoints"]["items"] == {"c1": {"checkpoint_id": "c1"}}
    assert agent.session["resume_state"] == {}
    assert agent.session["recovery"]["current_checkpoint_id"] == ""
```

- [ ] **Step 2: Confirm new sessions and reset still use legacy state**

```bash
uv run pytest tests/test_pico.py -k 'v3_messages_only or repeated_tool_detection or reset_clears_transient' -q
```

Expected: failures for schema 2/history, history-based repeat detection, and reset leaving messages/pointers behind.

- [ ] **Step 3: Create and normalize only v3 runtime sessions**

In `Pico.__init__`:

```python
if session is None:
    self.session = {
        "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "schema_version": 3,
        "created_at": now(),
        "workspace_root": workspace.repo_root,
        "messages": [],
        "recently_recalled": [],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {}},
        "checkpoints": {"current_id": "", "items": {}},
        "runtime_identity": {},
        "resume_state": {},
        "recovery": {"current_checkpoint_id": ""},
    }
else:
    self.session = migrate_session_to_v3(session)
```

Import `migrate_session_to_v3`. In `_ensure_session_shape` require a messages list and schema 3; do not create or normalize history.

- [ ] **Step 4: Stop the temporary v2 mirror**

Remove `legacy_history` from `_commit_session` and from all callers. The helper updates only candidate `messages` and non-transcript session state.

Delete all history initialization, append, save, and report logic from runtime and AgentLoop.

- [ ] **Step 5: Scan canonical tool blocks for repeat detection**

Replace `Pico.repeated_tool_call`:

```python
def repeated_tool_call(self, name, args):
    tool_calls = []
    for message in self.session.get("messages", []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append(block)
    repeated_count = sum(
        1
        for block in tool_calls[-6:]
        if block.get("name") == name and block.get("input") == args
    )
    return repeated_count >= 2
```

- [ ] **Step 6: Implement copy-on-write reset**

Replace `Pico.reset` with:

```python
def reset(self):
    candidate = deepcopy(self.session)
    candidate["messages"] = []
    candidate["recently_recalled"] = []
    candidate.pop("_recall_errors", None)
    candidate["working_memory"] = {
        "task_summary": "",
        "recent_files": [],
    }
    candidate["memory"] = {"file_summaries": {}}
    checkpoints = candidate.setdefault("checkpoints", {"current_id": "", "items": {}})
    checkpoints["current_id"] = ""
    checkpoints.setdefault("items", {})
    candidate["resume_state"] = {}
    recovery = candidate.setdefault("recovery", {})
    recovery["current_checkpoint_id"] = ""
    saved_path = self.session_store.save(candidate)
    self.session = candidate
    self.session_path = saved_path
    self.memory = WorkingMemory(workspace_root=self.root)
    self.resume_state = {}
    self.last_request_metadata = {}
```

Do not delete checkpoint items, checkpoint files, run directories, memory files, or change the session id.

- [ ] **Step 7: Rename request metadata and delete legacy runtime APIs**

Rename `last_prompt_metadata` to `last_request_metadata` in runtime, AgentLoop, tests, benchmark clients, and report building. Remove runtime `last_completion_metadata`.

Delete:

```text
Pico.record
Pico.record_message
Pico.history_text
Pico.prompt
Pico.note_tool
Pico.parse
Pico.retry_notice
Pico.parse_xml_tool
Pico.parse_attrs
Pico.extract
Pico.extract_raw
```

Update test doubles that monkeypatch `record_message` to inspect the COW save payload instead.

- [ ] **Step 8: Delete the old parser and Provider message utility**

- Delete `pico/model_output_parser.py` and `tests/test_model_output_parser.py`; every useful parser case must already exist in `tests/test_action_codec.py`.
- Delete `pico/providers/message_utils.py` and `tests/test_provider_message_utils.py`; every Provider imports `strip_pico_meta` from `pico.messages` and its pure tests live in `tests/test_messages.py`.
- Remove `legacy_string_path` from `pyproject.toml`.
- Update `pico/cli_commands.py` documentation to describe the v3 invariant inspector, not dual-write drift.

- [ ] **Step 9: Replace remaining test setup with valid canonical messages**

Run:

```bash
rg -n '\.record_message\(|\.record\(|session\["history"\]|get\("history"\)' tests benchmarks pico --glob '*.py'
```

Outside `pico/session_store.py` and migration tests, replace each setup with either:

```python
agent.session["messages"].append({
    "role": role,
    "content": content,
    "_pico_meta": {"created_at": created_at},
})
```

or `make_tool_pair` for tool events. Save once with `agent.session_store.save(agent.session)` only when the test explicitly exercises disk reload.

The named sweep scope is the complete Task 15 **Files** list above. If this scan finds a prohibited reference in any other path, stop and amend the plan before editing or staging that path.

- [ ] **Step 10: Run the structural deletion gate**

```bash
test ! -e pico/model_output_parser.py
test ! -e pico/providers/message_utils.py
! rg -n 'session\["history"\]|\.get\("history"\)' pico --glob '*.py' --glob '!session_store.py'
! rg -n 'Pico\.record\(|Pico\.record_message\(|history_text\(|ContextManager\.build\(|_build_prompt_and_metadata|model_output_parser|legacy_string_path' pico tests benchmarks pyproject.toml
! rg -n 'parse_model_output|toolu_local_|uuid' pico/providers/fallback_adapter.py
! rg -n 'last_completion_metadata' pico/agent_loop.py pico/runtime.py pico/evaluation
! rg -n 'last_prompt_metadata|prompt_metadata' pico/agent_loop.py pico/runtime.py pico/evaluation benchmarks/perf tests
```

Expected: every command exits zero and prints no prohibited production reference.

- [ ] **Step 11: Run the Phase 6 full gate**

```bash
./scripts/check.sh
```

Expected: Ruff passes; all pytest tests pass with zero legacy skips.

- [ ] **Step 12: Commit v3 cutover and deletions**

```bash
git add pico/runtime.py pico/agent_loop.py pico/messages.py pico/cli_commands.py pyproject.toml
git add pico/evaluation/experiments_real.py pico/evaluation/experiments_synthetic.py pico/evaluation/experiments_recovery.py pico/evaluation/fixed_benchmark.py pico/evaluation/metrics_reports.py pico/evaluation/provider_benchmark.py benchmarks/perf/bench_build_v2.py
git add tests/e2e/test_full_turn_roundtrip.py tests/test_pico.py tests/test_agent_loop_v2_shape.py tests/test_agent_loop_digest.py tests/test_context_manager.py tests/test_config_context.py tests/test_run_store.py tests/test_runtime_report.py tests/test_safety_invariants.py tests/memory/test_runtime_wiring.py tests/test_clean_up.py
git add -u pico/model_output_parser.py pico/providers/message_utils.py tests/test_model_output_parser.py tests/test_provider_message_utils.py
git commit -m "refactor(session): cut runtime over to messages v3"
```

---

### Task 16: Rebuild the Live Harness Around Trace Truth and One Explicit Provider

**Files:**

- Modify: `benchmarks/live_e2e/run_live_session.py`
- Modify: `benchmarks/live_e2e/tests/test_assertions.py`
- Modify: `benchmarks/live_e2e/README.md`
- Test: `tests/test_config.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RunConfig:
    provider: Literal["anthropic", "deepseek"]
    model: str
    max_provider_calls: int
    max_total_tokens: int
    timeout_seconds: int
    reset: bool
    verbose: bool
```

The harness accepts one `--provider` value per process. It loads project `.env` first, uses existing `providers.defaults` canonical names/defaults, and does not add a runtime resolver.

- [ ] **Step 1: Add offline Provider/env and trace-aggregation tests**

Extend `_turn_result_stub` defaults with:

```python
usage_complete=True,
request_metadata_by_call=({},),
system_cache_keys=("cache-key",),
action_origins=("provider_text",),
actual_user_contents=("prompt",),
```

Then add to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
def test_parse_args_selects_exactly_one_supported_provider(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_live_session", "--provider", "deepseek"],
    )
    config = parse_args()
    assert config.provider == "deepseek"


def test_project_env_is_loaded_before_deepseek_settings(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join([
            "PICO_DEEPSEEK_API_KEY=sentinel-secret",
            "PICO_DEEPSEEK_MODEL=deepseek-test",
            "PICO_DEEPSEEK_API_BASE=https://example.invalid/anthropic",
        ]),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        load_project_env(tmp_path)
        settings = provider_settings("deepseek")
    assert settings["api_key"] == "sentinel-secret"
    assert settings["model"] == "deepseek-test"
    assert settings["base_url"] == "https://example.invalid/anthropic"


def test_trace_aggregation_sums_every_model_turn_usage(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(json.dumps(event) for event in [
            {
                "event": "model_turn",
                "request_metadata": {"system_cache_key": "k", "messages_count": 1},
                "completion_usage": {"input_tokens": 10, "output_tokens": 2},
            },
            {
                "event": "action_decoded",
                "action_type": "tool",
                "origin": "native_tool_use",
            },
            {
                "event": "model_turn",
                "request_metadata": {"system_cache_key": "k", "messages_count": 3},
                "completion_usage": {
                    "input_tokens": 20,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 8,
                },
            },
        ]),
        encoding="utf-8",
    )
    captured = read_turn_trace(trace)
    assert captured["provider_calls"] == 2
    assert captured["usage"]["input_tokens"] == 30
    assert captured["usage"]["output_tokens"] == 6
    assert captured["usage"]["cache_read_input_tokens"] == 8
    assert captured["usage_complete"] is True
    assert captured["action_origins"] == ["native_tool_use"]
    assert captured["system_cache_keys"] == ["k", "k"]


def test_missing_call_usage_is_unknown_and_fails_budget_gate(tmp_path):
    result = _turn_result_stub(
        usage_complete=False,
        provider_call_count_this_turn=1,
    )
    reason = _budget_exceeded(
        [result],
        RunConfig(
            provider="deepseek",
            model="m",
            max_provider_calls=9,
            max_total_tokens=99,
            timeout_seconds=99,
            reset=False,
            verbose=False,
        ),
        wall_start_ns=time.monotonic_ns(),
    )
    assert reason == "usage_unknown"


def test_report_cannot_pass_when_aborted_short_or_assertions_empty(tmp_path):
    reporter = Reporter(
        RunConfig("deepseek", "m", 9, 99, 99, False, False),
        tmp_path,
    )
    path = reporter.write_json(
        all_results=[],
        all_assertions={},
        config=reporter.config,
        totals={},
        wall_time_ms=1,
        aborted_reason="provider_error_turn_1",
        expected_turn_count=5,
        session_schema=3,
        git_head="abc",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["overall_pass"] is False
    assert payload["aborted_reason"] == "provider_error_turn_1"
```

Add a reporter test with `PICO_DEEPSEEK_API_KEY=sentinel-secret` and assert `sentinel-secret` is absent from the entire JSON report.

- [ ] **Step 2: Confirm current live harness uses mutable last-call state**

```bash
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

Expected: failures for missing provider selection/settings, missing trace aggregation, usage-unknown handling, and false-positive report prevention.

- [ ] **Step 3: Load project env and derive one Provider's settings**

Add these module imports:

```python
from pico.config import load_project_env, provider_env
from pico.providers.defaults import (
    API_KEY_ENV_NAMES,
    BASE_URL_ENV_NAMES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    MODEL_ENV_NAMES,
)
```

At the start of `main`:

```python
repo_root = Path.cwd()
load_project_env(repo_root)
config = parse_args()
```

In `parse_args` add:

```python
parser.add_argument(
    "--provider",
    choices=("anthropic", "deepseek"),
    default="deepseek",
)
parser.add_argument("--model", default=None)
```

After parsing:

```python
settings = provider_settings(args.provider)
return RunConfig(
    provider=args.provider,
    model=args.model or settings["model"],
    max_provider_calls=args.max_provider_calls,
    max_total_tokens=args.max_total_tokens,
    timeout_seconds=args.timeout_seconds,
    reset=args.reset,
    verbose=args.verbose,
)
```

Define a two-branch harness helper using the existing defaults mappings:

```python
def _mapped_env(names, default=""):
    return provider_env(
        names[0],
        legacy_names=names[1:],
        default=default,
    )


def provider_settings(provider):
    if provider not in {"anthropic", "deepseek"}:
        raise ValueError(f"unsupported live provider: {provider}")
    return {
        "api_key": _mapped_env(API_KEY_ENV_NAMES[provider]),
        "model": _mapped_env(
            MODEL_ENV_NAMES[provider],
            default=DEFAULT_MODELS[provider],
        ),
        "base_url": _mapped_env(
            BASE_URL_ENV_NAMES[provider],
            default=DEFAULT_BASE_URLS[provider],
        ),
    }
```

This is local live-harness wiring, not a new model resolver.

- [ ] **Step 4: Build the selected existing Anthropic-compatible client**

Replace `_make_anthropic_client` with:

```python
def make_live_client(config):
    settings = provider_settings(config.provider)
    inner = AnthropicCompatibleModelClient(
        model=config.model,
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        temperature=0.0,
        timeout=120,
    )
    return _SniffingProviderWrapper(inner)
```

`check_env` checks the selected settings key without printing it. The sniffer records only each actual last top-level plain user content and returns the inner `Response` unchanged. Delete sniffer `last_completion_metadata` mirroring.

- [ ] **Step 5: Aggregate all call usage and action origins from trace**

Add:

```python
_LIVE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def read_turn_trace(trace_path):
    events = [
        json.loads(line)
        for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    turns = [event for event in events if event.get("event") == "model_turn"]
    actions = [
        event
        for event in events
        if event.get("event") == "action_decoded"
    ]
    totals = {key: 0 for key in _LIVE_USAGE_KEYS}
    usage_complete = bool(turns)
    cache_keys = []
    request_metadata = []
    for turn in turns:
        usage = turn.get("completion_usage")
        if not isinstance(usage, dict):
            usage_complete = False
            usage = {}
        for required in ("input_tokens", "output_tokens"):
            value = usage.get(required)
            if not isinstance(value, int) or isinstance(value, bool):
                usage_complete = False
        for key in _LIVE_USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
        metadata = dict(turn.get("request_metadata") or {})
        request_metadata.append(metadata)
        cache_keys.append(str(metadata.get("system_cache_key", "")))
    return {
        "provider_calls": len(turns),
        "usage": totals,
        "usage_complete": usage_complete,
        "request_metadata": request_metadata,
        "system_cache_keys": cache_keys,
        "action_origins": [
            str(event.get("origin", ""))
            for event in actions
            if event.get("origin")
        ],
    }
```

In `TurnRunner.run_turn`, snapshot the sniffer call count before `pico.ask`. Afterward:

```python
captured = read_turn_trace(
    self.pico.run_store.trace_path(self.pico.current_task_state)
)
new_sniffer_calls = self.pico.model_client.calls[sniffer_before:]
actual_user_contents = [
    str(call.get("last_user_content", ""))
    for call in new_sniffer_calls
]
```

Populate `TurnResult` from `captured`. Remove `_count_provider_calls`, `_extract_first_prompt_and_counts`, Provider metadata fallbacks, and session-based guesses. Add fields:

```python
usage_complete: bool
request_metadata_by_call: tuple[dict, ...]
system_cache_keys: tuple[str, ...]
action_origins: tuple[str, ...]
actual_user_contents: tuple[str, ...]
```

- [ ] **Step 6: Make every guard use `RunConfig` and fail unknown usage**

Replace token/call checks with:

```python
def _budget_exceeded(all_results, config, wall_start_ns):
    if any(not result.usage_complete for result in all_results):
        return "usage_unknown"
    total_calls = sum(
        result.provider_call_count_this_turn for result in all_results
    )
    if total_calls > config.max_provider_calls:
        return (
            f"max_provider_calls exceeded "
            f"({total_calls}>{config.max_provider_calls})"
        )
    total_tokens = sum(
        int(result.usage.get("input_tokens", 0))
        + int(result.usage.get("output_tokens", 0))
        for result in all_results
    )
    if total_tokens > config.max_total_tokens:
        return (
            f"max_total_tokens exceeded "
            f"({total_tokens}>{config.max_total_tokens})"
        )
    elapsed_seconds = (time.monotonic_ns() - wall_start_ns) / 1e9
    if elapsed_seconds > config.timeout_seconds:
        return (
            f"timeout_seconds exceeded "
            f"({elapsed_seconds:.0f}>{config.timeout_seconds})"
        )
    return None
```

Update every AssertionEngine expected/actual value that currently hard-codes 15 calls or 200,000 tokens to read the config passed to the engine.

- [ ] **Step 7: Assert real native action, per-call injection, v3, cache, and terminal state**

Make one tool turn prompt explicit:

```python
(
    2,
    "Use the API-provided native read_file tool to read pico/runtime.py, then summarize it. Do not emit XML tool text.",
    "native_tool_roundtrip",
)
```

For that turn assert:

```python
assert "native_tool_use" in result.action_origins
assert result.provider_call_count_this_turn >= 2
assert result.usage_complete is True
assert all(
    result.user_prompt in content
    and "<system-reminder>" in content
    for content in result.actual_user_contents
)
assert all(result.system_cache_keys)
assert len(set(result.system_cache_keys)) == 1
```

Global assertions must validate:

- at least one `action_decoded.origin == native_tool_use`;
- session schema is 3 and has no history;
- every canonical tool_use has its immediate matching tool_result;
- every completed run TaskState/report/trace is terminal;
- total calls/tokens are within current `RunConfig`.

Text-protocol tool origin does not satisfy the native assertion.

- [ ] **Step 8: Make reports unable to pass vacuously**

Change `Reporter.write_json` to accept `aborted_reason`, `expected_turn_count`, `session_schema`, and `git_head`. Write:

```python
"git_head": git_head,
"python_version": sys.version,
"session_schema": session_schema,
"provider": config.provider,
"model": config.model,
"action_origin_summary": dict(Counter(
    origin
    for result in all_results
    for origin in result.action_origins
)),
"aborted_reason": aborted_reason or "",
```

Compute:

```python
completed_all_turns = len(all_results) == expected_turn_count
has_assertions = assertion_total > 0
payload["overall_pass"] = (
    aborted_reason is None
    and completed_all_turns
    and has_assertions
    and assertion_failed == 0
)
```

In `main`, compute and pass provenance without shell interpolation:

```python
git_head = subprocess.run(
    ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
report_path = reporter.write_json(
    all_results,
    all_assertions,
    config,
    totals,
    wall_time_ms,
    aborted_reason=aborted_reason,
    expected_turn_count=len(TURNS),
    session_schema=int(pico.session.get("schema_version", 0)),
    git_head=git_head,
)
overall_pass = bool(
    json.loads(report_path.read_text(encoding="utf-8"))["overall_pass"]
)
```

Import `subprocess` at module scope and remove the local import in `warn_if_dirty_working_tree`.

Import `Counter`. Do not serialize environment dictionaries, headers, Provider objects, or API keys.

- [ ] **Step 9: Align README with executable behavior**

Document only:

```bash
uv run python -m benchmarks.live_e2e.run_live_session --provider deepseek
uv run python -m benchmarks.live_e2e.run_live_session --provider anthropic
```

State that one command is selected per gate, project `.env` is loaded, and canonical env names are:

```text
PICO_DEEPSEEK_API_KEY / PICO_DEEPSEEK_MODEL / PICO_DEEPSEEK_API_BASE
PICO_ANTHROPIC_API_KEY / PICO_ANTHROPIC_MODEL / PICO_ANTHROPIC_API_BASE
```

Do not claim both are run.

- [ ] **Step 10: Run all live-harness tests offline**

```bash
uv run pytest benchmarks/live_e2e/tests/test_assertions.py tests/test_config.py -q
```

Expected: all pass without network access or API credits.

- [ ] **Step 11: Commit the offline live gate**

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py benchmarks/live_e2e/README.md tests/test_config.py
git commit -m "test(live-e2e): verify one native provider from trace"
```

---

### Task 17: Regenerate Current-HEAD Evidence and Retire Stale Claims

**Files:**

- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json`
- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json`
- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json`
- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json`
- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/pico-benchmark-core-report.md`
- Create: `benchmarks/results/action-kernel-messages-v3-2026-07-10/DATA_PROVENANCE.md`
- Modify: `docs/review-pack/README.md`
- Modify: `docs/review-pack/dashboard.md`
- Modify: `benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md`
- Test: `tests/test_metrics.py`
- Test: `tests/test_evaluator.py`

**Interfaces:**

- Generated JSON is the evidence truth; Markdown is generated from those files or links to them.
- The 2026-06-07 directory remains immutable historical evidence and is explicitly labeled archived.
- No document claims that Task 19's real E2E passed before it actually runs.

- [ ] **Step 1: Run evidence-generator tests**

```bash
uv run pytest tests/test_metrics.py tests/test_evaluator.py tests/test_memory_quality_benchmark.py -q
```

Expected: all pass.

- [ ] **Step 2: Generate a fresh harness artifact**

```bash
mkdir -p benchmarks/results/action-kernel-messages-v3-2026-07-10
uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json")'
```

Expected: artifact summary has no failed tasks and every row uses `initial_messages_empty` rather than `initial_history_empty`.

- [ ] **Step 3: Generate context, memory, and recovery artifacts**

```bash
uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json", repetitions=3)'
```

Expected:

- context current-request preservation is 1.0 and bounded views are smaller than unbounded views;
- all memory variants record `bootstrap_tool_turn_dropped=true`;
- memory-on beats off and irrelevant on repeated reads and hit rate, with all correct rates 1.0;
- recovery artifact completes with no false-accept regression.

- [ ] **Step 4: Generate the core report from only the fresh artifacts**

```bash
uv run python -c 'from pico.evaluation.metrics import write_benchmark_core_report; write_benchmark_core_report(report_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/pico-benchmark-core-report.md", harness_artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json", context_artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json", memory_artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json", recovery_artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json")'
```

Expected: report terminology is bounded/unbounded sent-message chars and contains no legacy prompt/history claim.

- [ ] **Step 5: Write exact provenance**

Create `DATA_PROVENANCE.md` with:

```markdown
# Data Provenance

This directory is the C-stage Action Kernel and Messages v3 evidence set generated from the repository state immediately before the evidence-only commit.

Authoritative commands:

- `uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json")'`
- `uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json", repetitions=3)'`

Interpretation boundaries:

- Harness regression proves the deterministic runtime contract, not live Provider quality.
- Context ablation compares bounded and effectively unbounded actual request-message views.
- Memory ablation is valid only because every variant records that the bootstrap tool turn was dropped.
- Recovery ablation covers existing recovery behavior; it does not claim A-stage restore/security hardening.
- Real native Provider evidence is produced separately by the ignored local report in `benchmarks/live_e2e/results/`.
```

- [ ] **Step 6: Replace stale review-pack baselines**

In `docs/review-pack/README.md`:

- Remove stale fixed pass counts, stale “history” architecture wording, Provider smoke claims not produced by this plan, and worktree-triage prose.
- Link the new evidence directory.
- Describe architecture as `Response -> decode_action -> Action`, request overlay from canonical messages, and Session v3.
- State: “The real E2E is a separate final gate and is not claimed by this local evidence pack.”

In `docs/review-pack/dashboard.md` replace the stale sequential queue with these rows:

```markdown
| ID | Status | Acceptance | Evidence |
| --- | --- | --- | --- |
| C-01 Action boundary | Done | One decoder for native and fallback | action-codec and AgentLoop tests |
| C-02 Request truth | Done | Frozen injection and one-shot feedback | request-loop integration tests |
| C-03 Messages v3 | Done | Messages-only runtime and atomic migration | session migration/full test gate |
| C-04 Runtime integrity | Done | COW pair, truthful effects, terminal closure | runtime/tool/recovery tests |
| C-05 Local evidence | Done | Local quality, ablation, perf gates | current evidence directory |
| C-06 Real E2E | Pending final gate | One native DeepSeek or Anthropic tool turn | ignored local live report |
| A-stage security | Deferred | Separate approved design required after C | not in this implementation |
```

Add an archive notice to the top of `benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md`:

```markdown
> Archived historical snapshot. Do not use its prompt/history or memory-ablation numbers as current Messages v3 evidence.
```

- [ ] **Step 7: Verify evidence structure and forbidden terminology**

```bash
uv run python -c 'import json, pathlib; root=pathlib.Path("benchmarks/results/action-kernel-messages-v3-2026-07-10"); files=["harness-regression-v2.json","context-ablation-v2.json","memory-ablation-v2.json","recovery-ablation-v2.json"]; [json.loads((root/name).read_text()) for name in files]; print("valid json")'
! rg -n 'avg_full_prompt_chars|avg_raw_prompt_chars|initial_history_empty|session\["history"\]' benchmarks/results/action-kernel-messages-v3-2026-07-10 docs/review-pack
```

Expected: `valid json` and no forbidden-term output.

- [ ] **Step 8: Commit fresh evidence and review docs**

```bash
git add benchmarks/results/action-kernel-messages-v3-2026-07-10 docs/review-pack/README.md docs/review-pack/dashboard.md benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md
git commit -m "docs(evidence): regenerate messages v3 review pack"
```

---

### Task 18: Pass the Complete Local and Structural Gate

**Files:**

- Verification only; modify no source file.

**Interfaces:**

- Consumes the committed Tasks 1–17 tree.
- Produces local evidence that all deterministic gates pass before any paid/network call.

- [ ] **Step 1: Check patch hygiene without touching user-owned untracked files**

```bash
git diff --check
git status --short
```

Expected: `git diff --check` is silent. Status may still show the user's pre-existing untracked planning/spec/scratch files, but no uncommitted tracked implementation change.

- [ ] **Step 2: Run the final structural deletion suite**

```bash
test ! -e pico/model_output_parser.py
test ! -e pico/providers/message_utils.py
! rg -n 'session\["history"\]|\.get\("history"\)' pico --glob '*.py' --glob '!session_store.py'
! rg -n 'ContextManager\.build\(|_build_prompt_and_metadata|Pico\.record\(|Pico\.record_message\(|history_text\(|legacy_string_path' pico tests benchmarks pyproject.toml
! rg -n 'parse_model_output|toolu_local_|uuid' pico/providers/fallback_adapter.py
! rg -n 'last_completion_metadata' pico/agent_loop.py pico/runtime.py pico/evaluation benchmarks/live_e2e/run_live_session.py
! rg -n 'last_prompt_metadata|prompt_metadata|prompt_cache_key' pico/agent_loop.py pico/runtime.py pico/context_manager.py pico/evaluation benchmarks/live_e2e/run_live_session.py
! rg -n 'budget_reductions|prompt_chars|"sections"|section_order|section_budgets|history_chars' pico/agent_loop.py pico/runtime.py pico/context_manager.py
```

Expected: every command exits zero with no prohibited hit. Legacy Provider `complete` signatures that accept `prompt_cache_key` are intentionally outside the scan scope.

- [ ] **Step 3: Run the complete quality gate**

```bash
./scripts/check.sh
```

Expected: Ruff 0 errors and pytest all green with zero `legacy_string_path` skip.

- [ ] **Step 4: Run deterministic memory quality**

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

Expected: total 8, passed 8, failed 0.

- [ ] **Step 5: Regenerate the five-repetition memory ablation using the current default evidence path**

```bash
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2(repetitions=5)'
```

Expected:

- `memory_on.repeated_reads < memory_off.repeated_reads`;
- `memory_on.repeated_reads < memory_irrelevant.repeated_reads`;
- `memory_on.memory_hit_rate > memory_off.memory_hit_rate`;
- `memory_on.memory_hit_rate > memory_irrelevant.memory_hit_rate`;
- all three `correct_rate == 1.0`;
- every variant records `bootstrap_tool_turn_dropped == true`.

- [ ] **Step 6: Run all performance smoke commands**

```bash
uv run python -m benchmarks.perf.bench_build_v2
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
```

Expected: each command exits zero and prints parseable JSON. Do not introduce machine-specific latency thresholds.

- [ ] **Step 7: Re-run the offline live harness after all local evidence**

```bash
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

Expected: all pass with no network request.

- [ ] **Step 8: Record the local-gate checkpoint**

```bash
git status --short
git log -1 --oneline
```

Expected: no uncommitted tracked implementation/evidence file. Do not commit, delete, or stage the user's unrelated untracked files.

---

### Task 19: Run Exactly One Real Native-Tool E2E

**Files:**

- Generated and ignored: `benchmarks/live_e2e/results/live-e2e-*.json`
- Verification only; do not commit API keys or the ignored report.

**Interfaces:**

- Selects `deepseek` when `PICO_DEEPSEEK_API_KEY` is configured after project env loading; otherwise selects `anthropic` when `PICO_ANTHROPIC_API_KEY` is configured.
- Fails before network access when neither is configured.
- Executes one harness process and one Provider only.

- [ ] **Step 1: Select one configured Provider without printing a key**

```bash
PROVIDER=$(uv run python -c 'from pathlib import Path; import os; from pico.config import load_project_env; load_project_env(Path.cwd()); deepseek=bool(os.environ.get("PICO_DEEPSEEK_API_KEY","").strip()); anthropic=bool(os.environ.get("PICO_ANTHROPIC_API_KEY","").strip()); assert deepseek or anthropic, "no canonical live API key configured"; print("deepseek" if deepseek else "anthropic")')
test "$PROVIDER" = "deepseek" || test "$PROVIDER" = "anthropic"
```

Expected: prints/selects exactly one safe Provider name. It never prints a credential.

- [ ] **Step 2: Run the one authorized real E2E**

```bash
uv run python -m benchmarks.live_e2e.run_live_session --provider "$PROVIDER"
```

Expected: exit 0, all turns completed, non-empty assertions all passed, no abort reason, and at least one native `read_file` action. Do not run the other Provider afterward.

- [ ] **Step 3: Validate the newest ignored report**

```bash
REPORT=$(ls -t benchmarks/live_e2e/results/live-e2e-*.json | head -1)
uv run python -c 'import json, os, pathlib, sys; from pico.config import load_project_env; path=pathlib.Path(sys.argv[1]); payload=json.loads(path.read_text(encoding="utf-8")); load_project_env(pathlib.Path.cwd()); text=path.read_text(encoding="utf-8"); assert payload["overall_pass"] is True; assert payload["aborted_reason"] == ""; assert payload["session_schema"] == 3; assert payload["assertion_summary"]["total"] > 0; assert payload["assertion_summary"]["failed"] == 0; assert payload["action_origin_summary"].get("native_tool_use", 0) >= 1; assert payload["totals"]["provider_calls"] <= payload["config"]["max_provider_calls"]; assert payload["totals"]["input_tokens"] + payload["totals"]["output_tokens"] <= payload["config"]["max_total_tokens"]; names=("PICO_DEEPSEEK_API_KEY","PICO_ANTHROPIC_API_KEY"); assert all(not os.environ.get(name) or os.environ[name] not in text for name in names); print(path)' "$REPORT"
```

Expected: prints only the report path after all assertions pass.

- [ ] **Step 4: Verify fixture restoration and terminal artifacts**

```bash
test ! -e benchmarks/live_e2e/results/pre-run-pico.toml.bak
git diff --check
git status --short
```

Expected: no leaked fixture backup, no tracked live-result JSON, and no new tracked diff. Pre-existing user untracked files remain untouched.

- [ ] **Step 5: Stop**

Do not run a Provider matrix, retry the other Provider for comparison, publish, push, or begin A-stage work. Report the local gate, selected Provider/model, report path, native action count, call/token totals, and any intentionally retained untracked files.

---

## Requirement-to-Task Traceability

| Approved requirement | Owning task(s) |
| --- | --- |
| Pure total Action contract and unknown stop | 2 |
| Generic canonical messages | 3 |
| v3 selection rules | 4 |
| Report/session/completion metric separation | 5, 8 |
| CLI and fixed consumers | 6 |
| Actual request-view context evaluation | 7 |
| Atomic native/fallback codec switch | 8 |
| Preflight, frozen snapshot, feedback, metadata, legacy build deletion | 9 |
| Working summaries and valid ablation | 10 |
| COW tool pair and persistence stop | 11 |
| Tool statuses, effects, read-only, Ctrl-C Tool Change | 12 |
| Provider/runtime/interrupt/finalizer closure | 13 |
| Locked backup/atomic migration | 14 |
| v3 cutover, reset, repeated calls, direct legacy deletion | 15 |
| One-Provider offline live harness | 16 |
| Current evidence and stale-claim retirement | 17 |
| Full local/structural gates | 18 |
| One real native-tool E2E | 19 |
| A scope and model-connection work excluded | Global Constraints, 19 Step 5 |

## Plan Self-Review Gate

Before implementation begins, verify this plan itself:

```bash
rg -n 'TO[D]O|TB[D]|FIXM[E]|sa[m]e as|simila[r] to|and so o[n]|et[c]\.' docs/superpowers/plans/2026-07-10-pico-action-kernel-messages-v3.md
rg -n '^### Task ' docs/superpowers/plans/2026-07-10-pico-action-kernel-messages-v3.md
rg -n '^\*\*Interfaces:\*\*' docs/superpowers/plans/2026-07-10-pico-action-kernel-messages-v3.md
```

Expected:

- placeholder scan has no output;
- exactly 19 task headings;
- every implementation task has an Interfaces block; verification-only Tasks 18–19 still state their consumed/produced contract.

Then verify interface-name consistency:

```bash
rg -n 'decode_action|build_request_messages|build_v2|last_request_metadata|completion_usage_totals|SessionMigrationError|migrate_session_to_v3|effect_class|runtime_feedback_present' docs/superpowers/plans/2026-07-10-pico-action-kernel-messages-v3.md
```

Expected: one spelling for every named interface; no `ActionCodec` class, separate `model_actions.py`, Provider registry, or `history` runtime API is introduced.

## Execution Handoff

After this plan is approved for execution, choose one:

1. **Subagent-Driven (recommended):** stay in this task, use `superpowers:subagent-driven-development`, execute one plan task per fresh worker, and review each task before continuing.
2. **Inline Execution:** start a separate implementation task with `superpowers:executing-plans` and execute the checked tasks sequentially with the listed phase gates.

Do not mix both execution modes in one implementation run. In either mode, stop after Task 19; A-stage security work requires its own design and plan.
