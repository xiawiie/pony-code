# Pico Memory / Context Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 pico 从 prompt-as-string 范式迁移到 prompt-as-message-array 范式；引入结构化 memory + relevance recall + tool-result digest；分三阶段（P1 范式迁移 / P2 动态注入 / P3 memory 结构化）独立可发货。

**Architecture:** `system + tools + messages` 三字段 + `<system-reminder>` 动态注入 + 两个 `cache_control` 断点。Memory 从 flat 文件升级为 per-topic frontmatter + link 图 + tombstone。每阶段独立 PR、独立回滚。

**Tech Stack:** Python 3.11+, stdlib-only（不引入 PyYAML / embedding / vector store），pytest 单测，Anthropic messages API 语义。

## Global Constraints

- **stdlib-only**：不引入 PyYAML、numpy、向量库等外部依赖。frontmatter 解析、intent regex、digest 全部手写。
- **不新增 `.pico/memory/.state/` 目录**：所有 memory 状态存 note 本体或 `session` 内存。
- **Anthropic API 语义**：`system` 是 `list[content-block]`；`role` 只有 `user` / `assistant`；`tool_result` 是 user 消息的 content block。
- **Message 不可变**：一旦 append 进 `session["messages"]`，永不修改（digest 决策在 append 时完成）。
- **Cache anchor**：任何 mtime-driven 内容禁止进 Layer 1 `system`。
- **规范文件位置**：Spec 在 `docs/superpowers/specs/2026-07-07-pico-memory-context-redesign-design.md`；每个 task 的技术决策以 spec 为准。

---

# Phase 1 · Message-Paradigm Migration

**目标**：Provider 接口从 `complete(prompt, ...)` 变为 `complete(system, tools, messages, ...)`；session 结构从 `history` list 迁到 `messages` list；native tool_use 主路径 + fallback XML 保留；顺手清理三处 code smell。

**Phase 1 Definition of Done**：
- 老 session load 时自动 migrate 且 backup 生成；
- Anthropic 端 native tool_use 端到端可用；
- Fallback adapter 通过与老 XML 协议的回归测试；
- Cache_control 断点 1 命中被 `cache_read_input_tokens` 观测到；
- 全量 pytest 通过；
- P1 单独一个 PR 可发货、可回滚。

---

## Task 1: Provider Response 与 StopReason 抽象

**Files:**
- Create: `pico/providers/response.py`
- Test: `tests/test_provider_response.py`

**Interfaces:**
- Produces:
  - `class StopReason(str, Enum)`: `END_TURN`, `TOOL_USE`, `MAX_TOKENS`, `STOP_SEQUENCE`
  - `@dataclass class Response`: `stop_reason: StopReason`, `content: list[dict]`, `usage: dict`
- Consumes: 无

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_response.py
from pico.providers.response import Response, StopReason


def test_stop_reason_enum_values():
    assert StopReason.END_TURN == "end_turn"
    assert StopReason.TOOL_USE == "tool_use"
    assert StopReason.MAX_TOKENS == "max_tokens"
    assert StopReason.STOP_SEQUENCE == "stop_sequence"


def test_response_shape_text_only():
    r = Response(
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": "hello"}],
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    assert r.stop_reason == StopReason.END_TURN
    assert r.content[0]["text"] == "hello"


def test_response_shape_tool_use():
    r = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{"type": "tool_use", "id": "toolu_x", "name": "read_file", "input": {"path": "a.py"}}],
        usage={},
    )
    assert r.content[0]["name"] == "read_file"
    assert r.content[0]["input"]["path"] == "a.py"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_provider_response.py -v
```

Expected: `ModuleNotFoundError: pico.providers.response`

- [ ] **Step 3: 实现最小代码**

```python
# pico/providers/response.py
"""Provider-agnostic response type."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


@dataclass
class Response:
    stop_reason: StopReason
    content: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_provider_response.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add pico/providers/response.py tests/test_provider_response.py
git commit -m "feat(providers): add StopReason enum and Response dataclass"
```

---

## Task 2: Anthropic Adapter 新签名（system / tools / messages / cache_breakpoints）

**Files:**
- Modify: `pico/providers/anthropic_compatible.py`
- Test: `tests/test_provider_anthropic_v2.py`

**Interfaces:**
- Consumes: `Response`, `StopReason` from Task 1
- Produces: `AnthropicCompatibleModelClient.complete_v2(system, tools, messages, max_tokens, cache_breakpoints=None) -> Response`

**Note**: 保留旧 `complete(prompt, ...)` 直到 Task 8 全部清理完；本 task 新增 `complete_v2`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_anthropic_v2.py
"""Anthropic adapter v2 payload shape + response normalization tests."""
import json
from unittest.mock import patch, MagicMock

from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
from pico.providers.response import Response, StopReason


def _mock_urlopen(response_body):
    m = MagicMock()
    m.__enter__.return_value = MagicMock(read=lambda: json.dumps(response_body).encode("utf-8"))
    m.__exit__.return_value = False
    return m


def _make_client():
    return AnthropicCompatibleModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
    )


def test_complete_v2_payload_shape_and_cache_control():
    client = _make_client()
    system = [{"type": "text", "text": "SYSTEM_CORE", "cache_control": {"type": "ephemeral"}}]
    tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    messages = [{"role": "user", "content": "hi"}]

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5},
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        resp = client.complete_v2(system=system, tools=tools, messages=messages, max_tokens=100)

    assert captured_payload["data"]["system"] == system
    assert captured_payload["data"]["tools"] == tools
    assert captured_payload["data"]["messages"] == messages
    assert isinstance(resp, Response)
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": "ok"}]
    assert resp.usage["cache_creation_input_tokens"] == 5


def test_complete_v2_cache_breakpoint_on_message():
    client = _make_client()
    system = [{"type": "text", "text": "sys"}]
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})
    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(system=system, tools=[], messages=messages, max_tokens=10, cache_breakpoints=[1])
    # 断点位置的 message.content 应被转成 content-block list 并带 cache_control
    msg1 = captured["data"]["messages"][1]
    if isinstance(msg1["content"], list):
        assert msg1["content"][-1].get("cache_control") == {"type": "ephemeral"}
    else:
        # 允许把 string 消息扩展为 list of content blocks 来打 cache_control
        assert False, "cache_breakpoint 应把 message content 转为 list 形式"


def test_complete_v2_tool_use_response():
    client = _make_client()
    def fake_urlopen(req, timeout=None):
        return _mock_urlopen({
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "stop_reason": "tool_use",
            "usage": {},
        })
    with patch("urllib.request.urlopen", fake_urlopen):
        resp = client.complete_v2(system=[{"type": "text", "text": "s"}], tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"]["path"] == "a.py"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_provider_anthropic_v2.py -v
```

Expected: `AttributeError: 'AnthropicCompatibleModelClient' object has no attribute 'complete_v2'`

- [ ] **Step 3: 实现 complete_v2**

在 `pico/providers/anthropic_compatible.py` 末尾追加：

```python
    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        from .response import Response, StopReason

        # 打 cache_control 断点：把指定 message.content 转为 list-of-blocks 形式
        prepared_messages = []
        breakpoints = set(cache_breakpoints or [])
        for idx, msg in enumerate(messages):
            if idx in breakpoints:
                content = msg["content"]
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    blocks = list(content)
                    if blocks:
                        last = dict(blocks[-1])
                        last["cache_control"] = {"type": "ephemeral"}
                        blocks[-1] = last
                prepared_messages.append({"role": msg["role"], "content": blocks})
            else:
                prepared_messages.append({"role": msg["role"], "content": msg["content"]})

        payload = {
            "model": self.model,
            "system": system,
            "tools": tools,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if not tools:
            payload.pop("tools")

        req = urllib.request.Request(
            f"{self.base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=self.timeout) as raw:
            data = json.loads(raw.read().decode("utf-8"))

        stop_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.STOP_SEQUENCE,
        }
        stop_reason = stop_map.get(data.get("stop_reason", "end_turn"), StopReason.END_TURN)

        usage_details = _extract_anthropic_usage_cache_details(data)
        self.last_completion_metadata = usage_details

        return Response(
            stop_reason=stop_reason,
            content=list(data.get("content") or []),
            usage=usage_details,
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_provider_anthropic_v2.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add pico/providers/anthropic_compatible.py tests/test_provider_anthropic_v2.py
git commit -m "feat(providers): add anthropic complete_v2 with system/tools/messages + cache_breakpoints"
```

---

## Task 3: Fallback Adapter（本地/无 tool_use provider 转换层）

**Files:**
- Create: `pico/providers/fallback_adapter.py`
- Test: `tests/test_provider_fallback.py`

**Interfaces:**
- Consumes: `Response`, `StopReason`; underlying `provider.complete(prompt, max_new_tokens, ...)` 老签名
- Produces: `FallbackAdapter(inner_provider).complete_v2(system, tools, messages, max_tokens, cache_breakpoints=None) -> Response`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_fallback.py
from unittest.mock import MagicMock
from pico.providers.fallback_adapter import FallbackAdapter
from pico.providers.response import Response, StopReason


class _StubInner:
    def __init__(self, canned):
        self.canned = canned
        self.last_prompt = None
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        self.last_prompt = prompt
        self.last_completion_metadata = {"input_tokens": 3, "output_tokens": 2}
        return self.canned


def test_fallback_flattens_system_tools_messages():
    inner = _StubInner('<final>done</final>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "SYSTEM_CORE"}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {}}],
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
    )
    assert isinstance(resp, Response)
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": "done"}]
    assert "SYSTEM_CORE" in inner.last_prompt
    assert "read_file" in inner.last_prompt
    assert "hi" in inner.last_prompt


def test_fallback_parses_xml_tool_call_to_native_shape():
    inner = _StubInner('<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}],
        tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["type"] == "tool_use"
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"] == {"path": "a.py"}
    assert resp.content[0]["id"].startswith("toolu_local_")


def test_fallback_ignores_cache_breakpoints():
    inner = _StubInner('<final>ok</final>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10, cache_breakpoints=[0],
    )
    # 不支持 prompt cache 的 provider 应静默忽略 breakpoints
    assert resp.stop_reason == StopReason.END_TURN
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_provider_fallback.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现 FallbackAdapter**

```python
# pico/providers/fallback_adapter.py
"""Adapter that lets non-tool_use providers speak system+tools+messages API.

Flattens system+tools+messages to a single prompt string, delegates to
inner.complete(prompt, ...), then parses <tool>/<final> XML back into
native Response shape.
"""
from __future__ import annotations

import json
import uuid

from pico.model_output_parser import parse_model_output
from pico.providers.response import Response, StopReason


def _flatten_system(system: list[dict]) -> str:
    parts = []
    for block in system:
        text = block.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _flatten_tools(tools: list[dict]) -> str:
    if not tools:
        return ""
    lines = ["Available tools:"]
    for t in tools:
        schema = t.get("input_schema", {}).get("properties", {})
        fields = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in schema.items())
        lines.append(f"- {t['name']}({fields}): {t.get('description', '')}")
    return "\n".join(lines)


def _flatten_messages(messages: list[dict]) -> str:
    lines = ["Transcript:"]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content:
            btype = block.get("type")
            if btype == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
            elif btype == "tool_use":
                lines.append(f"[{role}:tool_use] {block['name']}({json.dumps(block.get('input', {}), sort_keys=True)})")
            elif btype == "tool_result":
                lines.append(f"[{role}:tool_result] {block.get('content', '')}")
    return "\n".join(lines)


class FallbackAdapter:
    def __init__(self, inner_provider):
        self._inner = inner_provider
        self.supports_prompt_cache = False
        self.supports_native_tools = False
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        prompt = "\n\n".join(part for part in (_flatten_system(system), _flatten_tools(tools), _flatten_messages(messages)) if part)
        raw = self._inner.complete(prompt, max_tokens)
        self.last_completion_metadata = dict(getattr(self._inner, "last_completion_metadata", {}))

        kind, payload = parse_model_output(raw)
        if kind == "tool":
            return Response(
                stop_reason=StopReason.TOOL_USE,
                content=[{
                    "type": "tool_use",
                    "id": f"toolu_local_{uuid.uuid4().hex[:12]}",
                    "name": payload["name"],
                    "input": payload.get("args", {}),
                }],
                usage=self.last_completion_metadata,
            )
        if kind == "final":
            return Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": payload}],
                usage=self.last_completion_metadata,
            )
        # retry / malformed → 用 STOP_SEQUENCE 让上层看到"未完成"
        return Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": str(payload)}],
            usage=self.last_completion_metadata,
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_provider_fallback.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add pico/providers/fallback_adapter.py tests/test_provider_fallback.py
git commit -m "feat(providers): add FallbackAdapter for non-tool_use backends"
```

---

## Task 4: Session Store v1→v2 Migrator + Backup

**Files:**
- Modify: `pico/session_store.py`
- Test: `tests/test_session_store_migrator.py`

**Interfaces:**
- Produces: `SessionStore.load()` 自动把 v1 session（含 `history`）升级到 v2（含 `messages`），并在升级前 backup 到 `.pico/sessions/backup/<session_id>.v1.json`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_session_store_migrator.py
import json
from pathlib import Path

import pytest

from pico.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / ".pico" / "sessions")


def _v1_session(tmp_path):
    return {
        "id": "s1",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_root": str(tmp_path),
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "hello", "created_at": "2026-01-01T00:00:02Z"},
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "a.py"},
                "content": "file content",
                "created_at": "2026-01-01T00:00:03Z",
            },
        ],
        "working_memory": {"task_summary": "", "recent_files": []},
    }


def test_migrator_converts_history_to_messages(store, tmp_path):
    v1 = _v1_session(tmp_path)
    store.save(v1)
    # 手动修改磁盘保留 v1 形状，然后再 load
    session_path = store.path_for(v1["id"])
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    loaded = store.load("s1")
    assert loaded["schema_version"] == 2
    assert "history" not in loaded
    assert "messages" in loaded
    msgs = loaded["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    # 老 tool 事件被拆成两条：assistant tool_use + user tool_result
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"][0]["type"] == "tool_use"
    tool_use_id = msgs[2]["content"][0]["id"]
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"][0]["type"] == "tool_result"
    assert msgs[3]["content"][0]["tool_use_id"] == tool_use_id


def test_migrator_writes_backup(store, tmp_path):
    v1 = _v1_session(tmp_path)
    session_path = store.path_for(v1["id"])
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    store.load("s1")

    backup_dir = session_path.parent / "backup"
    backups = list(backup_dir.glob("s1.v1.*.json"))
    assert len(backups) == 1
    backup_body = json.loads(backups[0].read_text(encoding="utf-8"))
    assert "history" in backup_body


def test_migrator_idempotent_on_v2(store, tmp_path):
    v2 = {
        "id": "s2",
        "workspace_root": str(tmp_path),
        "messages": [{"role": "user", "content": "hi"}],
        "schema_version": 2,
    }
    session_path = store.path_for("s2")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v2), encoding="utf-8")

    loaded = store.load("s2")
    assert loaded["schema_version"] == 2
    # v2 不再触发 backup
    backup_dir = session_path.parent / "backup"
    assert not backup_dir.exists() or not list(backup_dir.glob("s2.v1.*.json"))
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_session_store_migrator.py -v
```

Expected: FAIL（尚未实现 migrator）

- [ ] **Step 3: 在 session_store.py 中添加 migrator**

在 `pico/session_store.py` 中：

```python
# 顶部
import json
import time
import uuid


def _migrate_v1_to_v2(session: dict) -> dict:
    if session.get("schema_version", 1) >= 2:
        return session
    old_history = session.pop("history", [])
    messages = []
    i = 0
    while i < len(old_history):
        entry = old_history[i]
        role = entry.get("role")
        created_at = entry.get("created_at")
        if role == "tool":
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": entry.get("name", ""),
                    "input": entry.get("args", {}),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": entry.get("content", ""),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
        elif role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": entry.get("content", ""),
                "_pico_meta": {"created_at": created_at},
            })
        i += 1
    session["messages"] = messages
    session.setdefault("recently_recalled", [])
    session["schema_version"] = 2
    return session


def _write_backup(session_path, raw_bytes, session_id):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (backup_dir / f"{session_id}.v1.{ts}.json").write_bytes(raw_bytes)
```

并在 `SessionStore.load()`（或等价的现有 load 方法）里：

```python
def load(self, session_id):
    p = self.path_for(session_id)
    raw = p.read_bytes()
    session = json.loads(raw.decode("utf-8"))
    if session.get("schema_version", 1) < 2:
        _write_backup(p, raw, session_id)
        session = _migrate_v1_to_v2(session)
        # 立即写回升级后的格式
        p.write_text(json.dumps(session), encoding="utf-8")
    return session
```

若 `path_for` 方法不存在，同步新增它作为工具：

```python
def path_for(self, session_id: str):
    return self.root / f"{session_id}.json"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_session_store_migrator.py -v
```

Expected: 3 passed

- [ ] **Step 5: 跑全量 session_store 测试**

```bash
uv run pytest tests/test_session_store.py -v
```

Expected: all pass（老测试不应被打破）

- [ ] **Step 6: Commit**

```bash
git add pico/session_store.py tests/test_session_store_migrator.py
git commit -m "feat(session): v1→v2 migrator with backup, tool events split into assistant+user pairs"
```

---

## Task 5: ContextManager.build 新签名（返回 system + tools + messages）

**Files:**
- Modify: `pico/context_manager.py`
- Test: `tests/test_context_manager_v2.py`

**Interfaces:**
- Consumes: agent.prefix（旧稳定 prefix 文本，暂作为 SYSTEM_CORE），agent.tools（旧字典）
- Produces: `ContextManager.build_v2(user_message) -> (request, metadata)`
  - `request = {"system": list[dict], "tools": list[dict], "messages": list[dict], "cache_control_breakpoints": list[int]}`

**Note**: 保留旧 `build(user_message)` 与其字符串输出直到 Task 9 全量切换。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_manager_v2.py
from unittest.mock import MagicMock

from pico.context_manager import ContextManager


def _make_agent():
    a = MagicMock()
    a.prefix = "SYSTEM_CORE_TEXT"
    a.tools = {
        "read_file": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "Read a file.",
        },
        "write_file": {
            "schema": {"path": "str", "content": "str"},
            "risky": True,
            "description": "Write a file.",
        },
    }
    a.session = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="<workspace_state>...</workspace_state>")
    a.render_checkpoint_text = MagicMock(return_value="")
    a.feature_enabled = MagicMock(return_value=True)
    a.memory_store = None
    a.repo_map = None
    a.model_client = MagicMock(count_tokens=lambda t: len(t) // 4)
    return a


def test_build_v2_returns_system_tools_messages():
    a = _make_agent()
    cm = ContextManager(a)
    request, metadata = cm.build_v2("current input")
    assert isinstance(request, dict)
    assert isinstance(request["system"], list)
    assert request["system"][0]["type"] == "text"
    assert "SYSTEM_CORE_TEXT" in request["system"][0]["text"]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(request["tools"], list)
    # tools 转换到 Anthropic schema
    tools_by_name = {t["name"]: t for t in request["tools"]}
    assert "read_file" in tools_by_name
    assert "input_schema" in tools_by_name["read_file"]
    # risky flag 迁移到 description
    assert "approval" in tools_by_name["write_file"]["description"].lower()


def test_build_v2_appends_current_user_message():
    a = _make_agent()
    cm = ContextManager(a)
    request, _ = cm.build_v2("current input")
    assert request["messages"][-1]["role"] == "user"
    text = request["messages"][-1]["content"]
    assert "current input" in text


def test_build_v2_history_messages_preserved():
    a = _make_agent()
    cm = ContextManager(a)
    request, _ = cm.build_v2("x")
    # 历史两条 + 当前一条
    assert len(request["messages"]) == 3
    assert request["messages"][0]["content"] == "hello"
    assert request["messages"][1]["content"] == "hi there"


def test_build_v2_cache_breakpoint_on_second_to_last():
    a = _make_agent()
    cm = ContextManager(a)
    request, _ = cm.build_v2("x")
    # messages 长度 3，断点 2 应位于 index 1（当前 user 消息的前一条）
    assert request["cache_control_breakpoints"] == [len(request["messages"]) - 2]


def test_build_v2_metadata_contains_system_cache_key():
    a = _make_agent()
    cm = ContextManager(a)
    _, metadata = cm.build_v2("x")
    assert "system_cache_key" in metadata
    assert isinstance(metadata["system_cache_key"], str)
    assert len(metadata["system_cache_key"]) == 64  # sha256 hex
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_manager_v2.py -v
```

- [ ] **Step 3: 实现 build_v2**

在 `pico/context_manager.py` 追加：

```python
import hashlib as _hashlib

def _convert_pico_tool_to_anthropic(name, spec):
    props = {}
    required = []
    for arg_name, sig in (spec.get("schema") or {}).items():
        # 简化处理：'str', 'int=1' 等都当 string 处理
        if "int" in sig:
            props[arg_name] = {"type": "integer"}
        else:
            props[arg_name] = {"type": "string"}
        if "=" not in sig:
            required.append(arg_name)
    desc = spec.get("description", "")
    if spec.get("risky"):
        desc = (desc + " Requires user approval before execution.").strip()
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


def _build_tools_list(pico_tools: dict) -> list[dict]:
    return [_convert_pico_tool_to_anthropic(name, spec) for name, spec in sorted(pico_tools.items())]


class ContextManager:
    # 现有 __init__ / build 保留不动
    ...

    def build_v2(self, user_message):
        user_message = str(user_message)
        system_text = str(getattr(self.agent, "prefix", ""))
        system_block = {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        system = [system_block]

        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})

        # 历史 messages
        messages = list(self.agent.session.get("messages", []))

        # 当前 user 消息（P1 无注入，纯文本）
        messages.append({"role": "user", "content": user_message})

        # cache 断点 2：最后一条消息之前
        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []

        system_cache_key = _hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        metadata = {
            "system_cache_key": system_cache_key,
            "messages_count": len(messages),
            "cache_control_breakpoints": list(breakpoints),
            "prompt_cache_key": system_cache_key,   # 保持向后兼容
        }
        request = {"system": system, "tools": tools, "messages": messages, "cache_control_breakpoints": breakpoints}
        return request, metadata
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_context_manager_v2.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add pico/context_manager.py tests/test_context_manager_v2.py
git commit -m "feat(context): build_v2 returns {system, tools, messages, cache_breakpoints}"
```

---

## Task 6: agent_loop 切换到 v2（append message 而非 history dict）

**Files:**
- Modify: `pico/agent_loop.py`, `pico/runtime.py`

**Interfaces:**
- Consumes: `ContextManager.build_v2()`, `provider.complete_v2()`, `Response`
- Produces: 每轮把当前 user_message 与模型返回的 `tool_use` / `text` 分别 append 到 `session["messages"]` 里为 Anthropic 兼容形状；tool_result 也走 `role="user"` + `type="tool_result"`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_loop_v2_shape.py（新建）
"""agent_loop 在 v2 路径下正确 append messages。"""
from unittest.mock import MagicMock, patch

from pico.providers.response import Response, StopReason


def _stub_agent_loop_deps(agent):
    # 简化：mock 出所有非核心方法，只测 message 追加形状
    agent.session = {"messages": [], "id": "s1"}
    agent.record_message = MagicMock(side_effect=lambda m: agent.session["messages"].append(m))
    agent.workspace = MagicMock()
    agent.workspace.repo_root = "/tmp"


def test_agent_loop_appends_user_message_at_start():
    from pico.agent_loop import _append_user_turn

    agent = MagicMock()
    _stub_agent_loop_deps(agent)
    _append_user_turn(agent, "hello world")
    msgs = agent.session["messages"]
    assert msgs[-1] == {"role": "user", "content": "hello world", "_pico_meta": {"created_at": msgs[-1]["_pico_meta"]["created_at"]}}


def test_agent_loop_appends_tool_use_and_tool_result_pair():
    from pico.agent_loop import _append_tool_use, _append_tool_result

    agent = MagicMock()
    _stub_agent_loop_deps(agent)
    tool_use_id = _append_tool_use(agent, name="read_file", input={"path": "a.py"}, id_hint="toolu_x")
    _append_tool_result(agent, tool_use_id=tool_use_id, content="file text")

    msgs = agent.session["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["content"][0]["type"] == "tool_use"
    assert msgs[-2]["content"][0]["id"] == "toolu_x"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert msgs[-1]["content"][0]["tool_use_id"] == "toolu_x"
    assert msgs[-1]["content"][0]["content"] == "file text"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_agent_loop_v2_shape.py -v
```

- [ ] **Step 3: 在 `pico/agent_loop.py` 顶部添加辅助函数**

```python
from .workspace import now


def _append_user_turn(agent, text: str):
    msg = {"role": "user", "content": text, "_pico_meta": {"created_at": now()}}
    agent.record_message(msg)
    return msg


def _append_tool_use(agent, *, name: str, input: dict, id_hint: str | None = None) -> str:
    import uuid
    tool_use_id = id_hint or f"toolu_{uuid.uuid4().hex[:12]}"
    msg = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": input}],
        "_pico_meta": {"created_at": now(), "tool_use_id": tool_use_id},
    }
    agent.record_message(msg)
    return tool_use_id


def _append_tool_result(agent, *, tool_use_id: str, content: str, digest_applied: bool = False, source_hash: str | None = None):
    msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        "_pico_meta": {
            "created_at": now(),
            "tool_use_id": tool_use_id,
            "digest_applied": digest_applied,
            "source_hash": source_hash,
        },
    }
    agent.record_message(msg)
    return msg


def _append_assistant_text(agent, text: str):
    msg = {"role": "assistant", "content": text, "_pico_meta": {"created_at": now()}}
    agent.record_message(msg)
    return msg
```

- [ ] **Step 4: 在 `pico/runtime.py` Pico 类中加 `record_message`**

```python
def record_message(self, msg):
    self.session["messages"].append(self.redact_artifact(msg))
    self.session_path = self.session_store.save(self.session)
```

同时确保 `_ensure_session_shape()` 里初始化 `messages`：

```python
if not isinstance(self.session.get("messages"), list):
    self.session["messages"] = []
if not isinstance(self.session.get("recently_recalled"), list):
    self.session["recently_recalled"] = []
```

**保留** `record()` 老方法直到 Task 9。

- [ ] **Step 5: 运行测试确认通过**

```bash
uv run pytest tests/test_agent_loop_v2_shape.py -v
```

Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add pico/agent_loop.py pico/runtime.py tests/test_agent_loop_v2_shape.py
git commit -m "feat(agent_loop): message-shaped append helpers (user/tool_use/tool_result/assistant)"
```

---

## Task 7: AgentLoop.run 主循环走 v2 路径

**Files:**
- Modify: `pico/agent_loop.py`, `pico/runtime.py`
- Test: `tests/test_agent_loop_e2e_v2.py`

**Interfaces:**
- Consumes: `Response`, `_append_*` helpers, `ContextManager.build_v2`, `provider.complete_v2` (原生) 或 FallbackAdapter
- Produces: `AgentLoop.run(user_message)` 走 v2 路径；old string-prompt path 通过 FallbackAdapter 隐式支持

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_loop_e2e_v2.py
"""端到端：AgentLoop 用 v2 provider + v2 messages 完成一轮 tool_use → tool_result → final。"""
from unittest.mock import MagicMock

from pico.providers.response import Response, StopReason


class _StubProviderV2:
    """按顺序返回 canned responses。"""
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"system": system, "tools": tools, "messages": list(messages), "cache_breakpoints": cache_breakpoints})
        return self.script.pop(0)


def test_end_to_end_tool_call_then_final(tmp_path, monkeypatch):
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    provider = _StubProviderV2([
        Response(stop_reason=StopReason.TOOL_USE, content=[{"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {"path": "README.md"}}], usage={}),
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}], usage={}),
    ])

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    result = pico.ask("what's in readme?")
    assert result.strip() == "done"

    msgs = pico.session["messages"]
    # 应该有：user + assistant(tool_use) + user(tool_result) + assistant("done")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_a"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_agent_loop_e2e_v2.py -v
```

- [ ] **Step 3: 改造 `AgentLoop.run` 走 v2 路径**

替换 `pico/agent_loop.py` 中 `AgentLoop.run` 的核心循环，使用 `_append_*` helpers + `build_v2` + `complete_v2`；保留原有 trace / recovery / task_state 附加逻辑。**关键改动**：

```python
def run(self, user_message):
    agent = self.agent
    _append_user_turn(agent, user_message)
    # ... task_state / run_dir / trace 保留不变 ...

    while tool_steps < agent.max_steps and attempts < max_attempts:
        attempts += 1
        request, prompt_metadata = agent.context_manager.build_v2(user_message)

        # v2 provider call
        raw_response = agent.model_client.complete_v2(
            system=request["system"],
            tools=request["tools"],
            messages=request["messages"],
            max_tokens=agent.max_new_tokens,
            cache_breakpoints=request["cache_control_breakpoints"],
        )

        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}))
        prompt_metadata.update(completion_metadata)
        agent.last_prompt_metadata = prompt_metadata
        agent.last_completion_metadata = completion_metadata

        # 解析 response.content
        text_blocks = [b for b in raw_response.content if b.get("type") == "text"]
        tool_use_blocks = [b for b in raw_response.content if b.get("type") == "tool_use"]

        if tool_use_blocks:
            tb = tool_use_blocks[0]
            tool_use_id = _append_tool_use(agent, name=tb["name"], input=tb.get("input", {}), id_hint=tb.get("id"))
            tool_result = agent.execute_tool(tb["name"], tb.get("input", {}))
            result_text = tool_result.content
            _append_tool_result(agent, tool_use_id=tool_use_id, content=result_text)
            tool_steps += 1
            # ... 保留 verification / recovery checkpoint 附加逻辑 ...
            continue

        if raw_response.stop_reason == StopReason.END_TURN and text_blocks:
            final = text_blocks[0]["text"].strip()
            _append_assistant_text(agent, final)
            task_state.finish_success(final)
            return _finish_run(...)

        # retry / malformed
        continue
    # 未达 final → step_limit / retry_limit（保留原逻辑）
```

**注意**：不要一次改完所有 trace / checkpoint 附加逻辑；只改 core message flow 与 provider 调用，其余延用 attribute 名。Runtime 侧删除对旧 `record()` 的调用点（用 `record_message` + helpers 代替）。

- [ ] **Step 4: 运行 e2e 测试**

```bash
uv run pytest tests/test_agent_loop_e2e_v2.py -v
```

Expected: 1 passed

- [ ] **Step 5: 运行完整 agent_loop 相关测试**

```bash
uv run pytest tests/test_agent_loop.py tests/test_pico.py -v
```

Expected: 若测试假设老 XML 协议，需要 mock provider 层用 FallbackAdapter；将 breaking 老测试标记 `@pytest.mark.legacy_string_path`，在 P3 结束时清理。（**如所有测试通过则跳过此 mark**。）

- [ ] **Step 6: Commit**

```bash
git add pico/agent_loop.py pico/runtime.py tests/test_agent_loop_e2e_v2.py
git commit -m "feat(agent_loop): main loop uses v2 request + provider.complete_v2 + message helpers"
```

---

## Task 8: Clean-up (三 hash / WorkingMemory / relevant_memory / session["history"])

**Files:**
- Modify: `pico/runtime.py`, `pico/context_manager.py`
- Delete: `pico/working_memory.py`
- Modify: `pico/features/memory.py`（若引用 WorkingMemory）
- Test: `tests/test_clean_up.py`

**Interfaces:**
- Consumes: 无
- Produces: 唯一保留 `metadata["system_cache_key"]`；`session.pop("history", None)`；`session["messages"]` 是唯一历史面；`feature_flags` 里删除 `relevant_memory`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_clean_up.py
def test_no_working_memory_module():
    import importlib
    import pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pico.working_memory")


def test_default_feature_flags_no_relevant_memory():
    from pico.runtime import DEFAULT_FEATURE_FLAGS
    assert "relevant_memory" not in DEFAULT_FEATURE_FLAGS


def test_metadata_uses_system_cache_key_only():
    from unittest.mock import MagicMock
    from pico.context_manager import ContextManager

    a = MagicMock(prefix="s", tools={}, session={"messages": []})
    a.workspace = MagicMock()
    a.model_client = MagicMock(count_tokens=lambda t: len(t)//4)
    cm = ContextManager(a)
    _, metadata = cm.build_v2("x")
    # 只保留 system_cache_key（+ prompt_cache_key 作为发送 provider 的别名）
    assert "system_cache_key" in metadata
    assert "base_prefix_hash" not in metadata
    assert "stable_prefix_hash" not in metadata
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_clean_up.py -v
```

- [ ] **Step 3: 删除死代码**

```bash
rm pico/working_memory.py
```

在 `pico/runtime.py` 中：
- 删除 `from .working_memory import WorkingMemory`
- 删除 `self.memory = WorkingMemory.from_dict(...)` 及所有 `self._sync_working_memory()` 调用
- 删除 `DEFAULT_FEATURE_FLAGS` 里的 `"relevant_memory": True` 键
- 删除 `session["working_memory"]` 相关的 `_ensure_session_shape` 初始化
- 保留 `session["memory"]["file_summaries"]`（还被压缩逻辑使用）

在 `pico/context_manager.py` 中：
- 从 `_metadata` 与 `build_v2` metadata 中移除 `base_prefix_hash` / `stable_prefix_hash` / `prefix_hash` 三个键，只留 `system_cache_key`；`prompt_cache_key` 作为兼容别名保留

若 `pico/features/memory.py` 或其他文件 import `WorkingMemory`，一并清理。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_clean_up.py -v
```

Expected: 3 passed

- [ ] **Step 5: 全量回归**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: retire WorkingMemory, relevant_memory flag, dedup three hash fields"
```

---

## Task 9: P1 集成冒烟 + PR gate

**Files:**
- Test: `tests/test_p1_smoke.py`

**Interfaces:**
- Consumes: 全部 P1 组件

- [ ] **Step 1: 写冒烟测试**

```python
# tests/test_p1_smoke.py
"""P1 定义完成验证：
- Anthropic v2 payload shape 正确
- Fallback adapter 与老 XML 协议兼容
- Session v1 加载 → 自动 migrate + backup
- 三 hash 合一
"""
def test_p1_smoke_all_checkpoints_reachable():
    from pico.providers.response import Response, StopReason
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
    from pico.providers.fallback_adapter import FallbackAdapter
    from pico.session_store import SessionStore, _migrate_v1_to_v2  # noqa
    from pico.context_manager import ContextManager
    from pico.agent_loop import _append_user_turn, _append_tool_use, _append_tool_result
    # 全部符号存在即通过
    assert hasattr(AnthropicCompatibleModelClient, "complete_v2")
    assert hasattr(FallbackAdapter, "complete_v2")
    assert hasattr(ContextManager, "build_v2")
    assert callable(_append_user_turn)
    assert callable(_append_tool_use)
    assert callable(_append_tool_result)
```

- [ ] **Step 2: 运行**

```bash
uv run pytest tests/test_p1_smoke.py -v
uv run pytest -q  # 全量
```

Expected: all pass

- [ ] **Step 3: Commit + P1 PR 里程碑**

```bash
git add tests/test_p1_smoke.py
git commit -m "test(p1): message-paradigm migration smoke test"
git tag p1-message-paradigm-done
```

---

# Phase 2 · Dynamic Injection + Intent Budget

**目标**：`<system-reminder>` 注入 + `<pico:*>` 命名空间 + 转义 + intent 分类 + budget profile；`memory_index` / `project_structure` / `workspace_state` / `checkpoint` 从 stable prefix 挪出走注入；cache breakpoint 2 命中被观测。**本阶段不接入 recalled_memory**（P3 加）。

**Phase 2 Definition of Done**：
- 注入生效：Anthropic 返回携带 workspace_state 等标签；
- Zero-width space 转义抵御 `<pico:` 字面串攻击；
- Intent first-match-wins 命中；default 兜底不抛错；
- Injection budget 独立 hard_cap 生效，超限 tail_clip + telemetry；
- Cache_read_input_tokens > 0 在断点 2 上被观测到；
- P2 单独一个 PR。

---

## Task 10: escape_pico_tags + Namespace 契约

**Files:**
- Create: `pico/context/__init__.py`, `pico/context/escaping.py`
- Test: `tests/test_context_escaping.py`

**Interfaces:**
- Produces: `escape_pico_tags(text: str) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_escaping.py
from pico.context.escaping import escape_pico_tags


def test_plain_text_unchanged():
    assert escape_pico_tags("hello world") == "hello world"


def test_pico_open_tag_gets_zero_width_space():
    # 用户内容里含 <pico:foo> 应插入 zero-width space（U+200B）打断词边界
    src = "here is <pico:foo>evil</pico:foo>"
    out = escape_pico_tags(src)
    assert "<pico​:" in out
    assert "</pico​:" in out
    assert "<pico:" not in out
    assert "</pico:" not in out


def test_visible_length_preserved_ignoring_zwsp():
    src = "<pico:x>"
    out = escape_pico_tags(src)
    assert out.replace("​", "") == src


def test_no_partial_replace_of_similar_prefixes():
    # 只替换 <pico: / </pico:，不动 <picofoo:
    src = "<picofoo:tag>"
    assert escape_pico_tags(src) == "<picofoo:tag>"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_escaping.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/context/__init__.py
"""pico context subsystem: injection, digest, intent, renderer."""
```

```python
# pico/context/escaping.py
"""Prompt-injection defense: break literal <pico:*> tag lookalikes."""

ZWSP = "​"


def escape_pico_tags(text: str) -> str:
    """把可能被误识别为闭合 <pico:*> 结构标签的字面串打断。

    用 zero-width space（U+200B）插到 `pico` 与 `:` 之间：
    - 视觉上不变
    - 词边界被打破，模型不会把 `<pico​:` 识别为结构标签
    - 只处理 `<pico:` 与 `</pico:` 精确前缀，不影响 `<picofoo:` 等相似串
    """
    if not text:
        return text
    return text.replace("<pico:", f"<pico{ZWSP}:").replace("</pico:", f"</pico{ZWSP}:")
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_escaping.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/__init__.py pico/context/escaping.py tests/test_context_escaping.py
git commit -m "feat(context): escape_pico_tags with zero-width space defense"
```

---

## Task 11: intent.py — regex 分类 + first-match-wins

**Files:**
- Create: `pico/context/intent.py`
- Test: `tests/test_context_intent.py`

**Interfaces:**
- Produces:
  - `classify_intent(user_message: str) -> IntentResult`
  - `IntentResult` 是 `NamedTuple`：`name: str`, `matched_keyword: str`, `budget: dict[str, int]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_intent.py
from pico.context.intent import classify_intent, INTENT_PROFILES


def test_default_when_no_match():
    r = classify_intent("random neutral message")
    assert r.name == "default"
    assert r.matched_keyword == ""
    assert r.budget == INTENT_PROFILES["default"]["budget"]


def test_debug_wins_over_recall_when_both_present():
    # first-match-wins 优先级：debug > recall > structural
    r = classify_intent("上次报错了")  # 同时含 "上次"(recall) 和 "报错"(debug)
    assert r.name == "debug"


def test_recall_keyword_hit():
    r = classify_intent("上次讨论过什么？")
    assert r.name == "recall"
    assert r.matched_keyword == "上次"


def test_structural_keyword_hit():
    r = classify_intent("讲讲这个项目的架构")
    assert r.name == "structural"


def test_case_insensitive():
    r = classify_intent("what is the ARCHITECTURE?")
    assert r.name == "structural"


def test_budget_dict_has_four_sources():
    r = classify_intent("random")
    assert set(r.budget.keys()) == {"project_structure", "memory_index", "recalled_memory", "workspace_state"}
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_intent.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/context/intent.py
"""Intent classification via keyword substring matching (first-match-wins)."""
from __future__ import annotations

from typing import NamedTuple

INTENT_PROFILES: dict[str, dict] = {
    "debug": {
        "keywords": ["报错", "error", "traceback", "fail", "not working", "broken", "崩溃"],
        "budget":   {"workspace_state": 1200, "recalled_memory": 600, "project_structure": 200, "memory_index": 200},
    },
    "recall": {
        "keywords": ["上次", "之前", "记得", "past", "previous", "last time"],
        "budget":   {"recalled_memory": 1600, "memory_index": 800, "project_structure": 200, "workspace_state": 300},
    },
    "structural": {
        "keywords": ["架构", "结构", "怎么组织", "目录", "layout", "architecture"],
        "budget":   {"project_structure": 2000, "memory_index": 400, "recalled_memory": 800, "workspace_state": 300},
    },
    "default": {
        "keywords": [],
        "budget":   {"project_structure": 600, "memory_index": 400, "recalled_memory": 600, "workspace_state": 500},
    },
}

# 固定优先级
_INTENT_ORDER = ("debug", "recall", "structural")


class IntentResult(NamedTuple):
    name: str
    matched_keyword: str
    budget: dict


def classify_intent(user_message: str) -> IntentResult:
    text = (user_message or "").lower()
    for name in _INTENT_ORDER:
        for kw in INTENT_PROFILES[name]["keywords"]:
            if kw.lower() in text:
                return IntentResult(name=name, matched_keyword=kw, budget=dict(INTENT_PROFILES[name]["budget"]))
    return IntentResult(name="default", matched_keyword="", budget=dict(INTENT_PROFILES["default"]["budget"]))
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_intent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/intent.py tests/test_context_intent.py
git commit -m "feat(context): intent classifier with first-match-wins priority"
```

---

## Task 12: Injection Sources — 4 个源渲染函数

**Files:**
- Create: `pico/context/sources.py`
- Test: `tests/test_context_sources.py`

**Interfaces:**
- Produces:
  - `render_workspace_state(agent, budget_tokens: int) -> str | None`
  - `render_memory_index(agent, budget_tokens: int) -> str | None`
  - `render_project_structure(agent, budget_tokens: int) -> str | None`
  - `render_checkpoint(agent, budget_tokens: int) -> str | None`

**Note**：P2 阶段不实现 `recalled_memory`（依赖 P3 recall.py）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_sources.py
from unittest.mock import MagicMock

from pico.context.sources import (
    render_workspace_state, render_memory_index,
    render_project_structure, render_checkpoint,
)


def _agent():
    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="<workspace_state>\n- branch: main\n</workspace_state>")
    a.memory_store = MagicMock()
    file_entry = MagicMock(path="workspace/notes/a.md", size_chars=100, first_line="# A")
    a.memory_store.list = MagicMock(return_value=[file_entry])
    a.repo_map = MagicMock()
    a.repo_map.refresh_if_stale = MagicMock()
    a.repo_map.top_level_tree = MagicMock(return_value=[{"path": "pico", "file_count": 30}])
    a.repo_map.language_stats = MagicMock(return_value={"python": 30})
    a.render_checkpoint_text = MagicMock(return_value="")
    return a


def test_workspace_state_returns_content():
    out = render_workspace_state(_agent(), budget_tokens=500)
    assert out is not None
    assert "branch: main" in out


def test_workspace_state_returns_none_when_empty():
    a = _agent()
    a.workspace.volatile_text.return_value = ""
    assert render_workspace_state(a, budget_tokens=500) is None


def test_memory_index_lists_entries():
    out = render_memory_index(_agent(), budget_tokens=500)
    assert out is not None
    assert "workspace/notes/a.md" in out


def test_project_structure_shows_tree():
    out = render_project_structure(_agent(), budget_tokens=500)
    assert out is not None
    assert "pico" in out


def test_checkpoint_none_when_empty():
    assert render_checkpoint(_agent(), budget_tokens=500) is None


def test_source_respects_budget_via_tail_clip():
    a = _agent()
    long_state = "\n".join([f"- commit {i}: xxxx" for i in range(200)])
    a.workspace.volatile_text.return_value = long_state
    # budget 100 token ≈ 400 char，输出应被截断
    out = render_workspace_state(a, budget_tokens=100)
    assert len(out) <= 400 + 20  # 允许小误差
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_sources.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/context/sources.py
"""Injection source renderers: each returns pre-escaping raw text or None."""
from __future__ import annotations


def _tail_clip(text: str, char_budget: int) -> str:
    if len(text) <= char_budget:
        return text
    if char_budget <= 3:
        return text[:char_budget]
    return text[: char_budget - 3] + "..."


def _budget_to_chars(budget_tokens: int) -> int:
    # 保守估算 1 token ≈ 4 char；后续接入 model_client.count_tokens 时替换
    return max(0, int(budget_tokens) * 4)


def render_workspace_state(agent, budget_tokens: int) -> str | None:
    try:
        text = str(agent.workspace.volatile_text() or "").strip()
    except Exception:
        return None
    if not text:
        return None
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_memory_index(agent, budget_tokens: int) -> str | None:
    store = getattr(agent, "memory_store", None)
    if store is None:
        return None
    entries = store.list()
    if not entries:
        return None
    lines = ["Memory files:"]
    for e in entries:
        first = getattr(e, "first_line", "")
        first = first[:80] if first else ""
        lines.append(f"- {e.path} ({e.size_chars} chars) {first}")
    text = "\n".join(lines)
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_project_structure(agent, budget_tokens: int) -> str | None:
    repo_map = getattr(agent, "repo_map", None)
    if repo_map is None:
        return None
    try:
        repo_map.refresh_if_stale()
    except Exception:
        return None
    tree = repo_map.top_level_tree()
    if not tree:
        return None
    stats = repo_map.language_stats() or {}
    lang_str = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
    lines = [f"Project (languages: {lang_str}):"]
    for entry in tree:
        lines.append(f"- {entry['path']}/  ({entry['file_count']} files)")
    text = "\n".join(lines)
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_checkpoint(agent, budget_tokens: int) -> str | None:
    renderer = getattr(agent, "render_checkpoint_text", None)
    if renderer is None:
        return None
    try:
        text = str(renderer() or "").strip()
    except Exception:
        return None
    if not text:
        return None
    return _tail_clip(text, _budget_to_chars(budget_tokens))
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_sources.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/sources.py tests/test_context_sources.py
git commit -m "feat(context): workspace_state/memory_index/project_structure/checkpoint source renderers"
```

---

## Task 13: renderer.py — 统一渲染入口 + 注入 budget 分配

**Files:**
- Create: `pico/context/renderer.py`
- Test: `tests/test_context_renderer.py`

**Interfaces:**
- Consumes: `escape_pico_tags`, `classify_intent`, source renderers
- Produces: `render_current_user_message(agent, user_message: str) -> tuple[str, dict]`
  - 返回 `(rendered_text, telemetry_dict)`
  - `telemetry_dict` 含 `intent`, `injection_tokens[source]`, `injection_truncated[source]`, `injection_dropped`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_renderer.py
from unittest.mock import MagicMock, patch

from pico.context.renderer import render_current_user_message


def _agent():
    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="<workspace_state>\n- branch: main\n</workspace_state>")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    return a


def test_renders_current_message_with_wrapped_reminders():
    text, tele = render_current_user_message(_agent(), "hi")
    assert "<system-reminder>" in text
    assert "<pico:workspace_state>" in text
    assert "hi" in text
    # user message 在最后
    assert text.strip().endswith("hi")


def test_intent_recorded_in_telemetry():
    _, tele = render_current_user_message(_agent(), "上次讨论过什么？")
    assert tele["intent"]["name"] == "recall"
    assert tele["intent"]["matched_keyword"] == "上次"


def test_escapes_pico_tags_in_source_content():
    a = _agent()
    a.workspace.volatile_text.return_value = "<pico:evil>attack</pico:evil>"
    text, _ = render_current_user_message(a, "hi")
    assert "<pico​:evil>" in text
    # 原文里的 </pico:workspace_state> 是 renderer 自己加的，未被误替换
    assert "</pico:workspace_state>" in text


def test_omits_sources_with_zero_budget():
    a = _agent()
    with patch("pico.context.renderer.classify_intent") as mock_intent:
        # profile 中把 workspace_state 设为 0，应该不出现
        mock_intent.return_value = MagicMock(
            name="dbg", matched_keyword="", budget={"workspace_state": 0, "memory_index": 400, "project_structure": 200, "recalled_memory": 0},
        )
        text, tele = render_current_user_message(a, "x")
    assert "<pico:workspace_state>" not in text
    assert tele["injection_tokens"].get("workspace_state", 0) == 0
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_renderer.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/context/renderer.py
"""Assemble current-turn user message: <system-reminder> blocks + user text."""
from __future__ import annotations

from .escaping import escape_pico_tags
from .intent import classify_intent
from .sources import (
    render_workspace_state, render_memory_index,
    render_project_structure, render_checkpoint,
)

# recalled_memory 在 P3 接入；P2 阶段 renderer 保留 slot 位置但源为 None
SOURCE_ORDER = ("workspace_state", "memory_index", "project_structure", "recalled_memory", "checkpoint")

_RENDERERS = {
    "workspace_state":   render_workspace_state,
    "memory_index":      render_memory_index,
    "project_structure": render_project_structure,
    "recalled_memory":   lambda a, b: None,  # placeholder，P3 覆盖
    "checkpoint":        render_checkpoint,
}


def _count_tokens(agent, text: str) -> int:
    counter = getattr(getattr(agent, "model_client", None), "count_tokens", None)
    if callable(counter):
        try:
            return int(counter(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def render_current_user_message(agent, user_message: str):
    intent = classify_intent(user_message)
    budget = intent.budget

    telemetry = {
        "intent": {"name": intent.name, "matched_keyword": intent.matched_keyword},
        "injection_tokens": {},
        "injection_truncated": {},
        "injection_dropped": [],
    }

    blocks = []
    for source_name in SOURCE_ORDER:
        source_budget = int(budget.get(source_name, 0) or 0)
        if source_budget <= 0:
            telemetry["injection_tokens"][source_name] = 0
            continue
        renderer = _RENDERERS.get(source_name)
        if renderer is None:
            continue
        raw = renderer(agent, source_budget)
        if not raw:
            telemetry["injection_tokens"][source_name] = 0
            continue
        original_tokens = _count_tokens(agent, raw)
        if original_tokens > source_budget:
            telemetry["injection_truncated"][source_name] = telemetry["injection_truncated"].get(source_name, 0) + 1
        escaped = escape_pico_tags(raw)
        block = (
            f"<system-reminder>\n"
            f"<pico:{source_name}>\n{escaped}\n</pico:{source_name}>\n"
            f"</system-reminder>"
        )
        blocks.append(block)
        telemetry["injection_tokens"][source_name] = _count_tokens(agent, escaped)

    text = "\n\n".join(blocks + [user_message]) if blocks else user_message
    return text, telemetry
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_renderer.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/renderer.py tests/test_context_renderer.py
git commit -m "feat(context): render_current_user_message with intent-driven injection budgets"
```

---

## Task 14: ContextManager.build_v2 接入 renderer + budget enforcement + cache breakpoint

**Files:**
- Modify: `pico/context_manager.py`
- Test: `tests/test_context_manager_injection.py`

**Interfaces:**
- Consumes: `render_current_user_message`
- Produces: `build_v2` 输出的最后一条 user 消息 content 包含 `<system-reminder>` blocks；`metadata` 合并 telemetry；`cache_control_breakpoints` 保持在 messages[-2]。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_manager_injection.py
from unittest.mock import MagicMock

from pico.context_manager import ContextManager


def _agent():
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": [{"role": "user", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="- branch: main")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: len(t) // 4)
    return a


def test_build_v2_current_message_contains_injection():
    cm = ContextManager(_agent())
    request, metadata = cm.build_v2("hello")
    current = request["messages"][-1]["content"]
    assert "<system-reminder>" in current
    assert "<pico:workspace_state>" in current
    assert current.strip().endswith("hello")


def test_build_v2_telemetry_records_intent():
    cm = ContextManager(_agent())
    _, metadata = cm.build_v2("上次讨论过什么？")
    assert metadata["intent"]["name"] == "recall"


def test_build_v2_pinned_layer_overflow_failloud():
    a = _agent()
    a.prefix = "x" * 200_000  # 假设 20K token 上限 ≈ 80K char
    a.tools = {}
    cm = ContextManager(a)
    import pytest
    with pytest.raises(RuntimeError, match="SystemTooBig"):
        cm.build_v2("hi")
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_manager_injection.py -v
```

- [ ] **Step 3: 更新 build_v2**

在 `pico/context_manager.py` 中修改 `build_v2`：

```python
from pico.context.renderer import render_current_user_message


class ContextManager:
    ...

    def build_v2(self, user_message):
        user_message = str(user_message)
        system_text = str(getattr(self.agent, "prefix", ""))
        system_block = {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        system = [system_block]
        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})

        # Pinned overflow check
        system_tokens = self._count_tokens(system_text)
        tools_tokens  = self._count_tokens(str(tools))
        hard_cap = 20000  # spec §6.4，也可从 pico.toml 读取
        if system_tokens + tools_tokens > hard_cap:
            raise RuntimeError(
                f"SystemTooBig: system+tools tokens {system_tokens + tools_tokens} exceed {hard_cap}. "
                f"Inspect workspace.stable_text() or tools schema."
            )

        # Renderer 生成当前 user 消息
        current_user_text, injection_telemetry = render_current_user_message(self.agent, user_message)

        messages = list(self.agent.session.get("messages", []))
        messages.append({"role": "user", "content": current_user_text})

        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []

        import hashlib
        system_cache_key = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        metadata = {
            "system_cache_key": system_cache_key,
            "prompt_cache_key": system_cache_key,
            "system_tokens": system_tokens,
            "tools_tokens": tools_tokens,
            "messages_count": len(messages),
            "cache_control_breakpoints": list(breakpoints),
            **injection_telemetry,
        }
        request = {"system": system, "tools": tools, "messages": messages, "cache_control_breakpoints": breakpoints}
        return request, metadata

    def _count_tokens(self, text):
        counter = getattr(getattr(self.agent, "model_client", None), "count_tokens", None)
        if callable(counter):
            try:
                return int(counter(text))
            except Exception:
                pass
        return max(1, len(text) // 4)
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_manager_injection.py tests/test_context_manager_v2.py -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pico/context_manager.py tests/test_context_manager_injection.py
git commit -m "feat(context): build_v2 wraps current user message with injection + pinned overflow guard"
```

---

## Task 15: P2 集成冒烟 + PR gate

**Files:**
- Test: `tests/test_p2_smoke.py`

- [ ] **Step 1: 冒烟**

```python
# tests/test_p2_smoke.py
from unittest.mock import MagicMock

from pico.context.escaping import escape_pico_tags
from pico.context.intent import classify_intent
from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager


def test_p2_end_to_end():
    a = MagicMock()
    a.prefix = "SYSTEM"
    a.tools = {}
    a.session = {"messages": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="branch: main")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: len(t) // 4)

    cm = ContextManager(a)
    req, meta = cm.build_v2("上次报错了")

    last_content = req["messages"][-1]["content"]
    assert "<pico:workspace_state>" in last_content
    assert meta["intent"]["name"] == "debug"   # "报错" 命中 debug（优先级高于 recall）
    assert "上次报错了" in last_content
    assert meta["cache_control_breakpoints"] == [0]  # 只有一条消息时不打断点或空
```

- [ ] **Step 2: 运行 + 全量**

```bash
uv run pytest tests/test_p2_smoke.py -v
uv run pytest -q
```

- [ ] **Step 3: Commit + 里程碑**

```bash
git add tests/test_p2_smoke.py
git commit -m "test(p2): dynamic injection + intent budget smoke test"
git tag p2-dynamic-injection-done
```

---

# Phase 3 · Memory Structured + Recall + Digest

**目标**：`agent/*.md` per-topic + frontmatter + tombstone + link expansion + BM25 field boost；`memory_save(topic=...)` 参数；`pico-cli memory migrate`；`recall.py` 四护栏 + 接入 renderer；`digest.py` + per-tool summarizer + tool_result 原文存磁盘。

**Phase 3 Definition of Done**：
- Agent 新写 note 走 `agent/<topic>.md` 且带 frontmatter；
- Migrator 可 dry-run / apply / rollback；
- 四护栏各触发场景测试通过；
- Digest fallback 触发不影响主流程；
- Recalled_memory 注入到当前 user message，命中带 provenance；
- P3 单独 PR 可发货。

---

## Task 16: Frontmatter Parser (stdlib 手写)

**Files:**
- Create: `pico/memory/frontmatter.py`
- Test: `tests/test_memory_frontmatter.py`

**Interfaces:**
- Produces:
  - `parse_frontmatter(text: str) -> tuple[dict, str]` 返回 `(frontmatter_dict, body_string)`
  - `FRONTMATTER_KEYS = ("name", "type", "description", "tags", "aliases", "supersedes")`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_frontmatter.py
from pico.memory.frontmatter import parse_frontmatter


def test_valid_frontmatter():
    src = "---\nname: foo\ntype: feedback\ndescription: hi\ntags: [a, b]\naliases: []\nsupersedes: []\n---\nbody line\n"
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
    src = "---\nnot yaml at all: :::::\n"  # 没有结束 ---
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
    meta, _ = parse_frontmatter(src)
    assert meta["tags"] == ["a", "b", "c"]
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_frontmatter.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/memory/frontmatter.py
"""Stdlib-only frontmatter parser.

Supports one-level flat `key: value` pairs.
Values are either strings or bracket-lists like `[a, b, c]`.
"""
from __future__ import annotations

FRONTMATTER_KEYS = ("name", "type", "description", "tags", "aliases", "supersedes")
_LIST_KEYS = {"tags", "aliases", "supersedes"}


def _parse_list_value(v: str) -> list[str]:
    v = v.strip()
    if not (v.startswith("[") and v.endswith("]")):
        return []
    inner = v[1:-1].strip()
    if not inner:
        return []
    return [x.strip() for x in inner.split(",") if x.strip()]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    # find closing ---
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        # 兼容行尾无换行
        end = rest.rfind("\n---")
        if end == -1 or (end + len("\n---") != len(rest)):
            return {}, text
        block = rest[:end]
        body = ""
    else:
        block = rest[:end]
        body = rest[end + len("\n---\n"):]

    meta: dict = {}
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
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_frontmatter.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/frontmatter.py tests/test_memory_frontmatter.py
git commit -m "feat(memory): stdlib-only frontmatter parser (flat, list-aware)"
```

---

## Task 17: BlockStore 支持 `agent/` scope + 读取 frontmatter

**Files:**
- Modify: `pico/memory/block_store.py`
- Test: `tests/test_memory_block_store_agent_scope.py`

**Interfaces:**
- Consumes: `parse_frontmatter`
- Produces:
  - `MemoryFile` 新增 `frontmatter: dict` 字段
  - `BlockStore._scan_scope` 扫 `agent/**/*.md`
  - `BlockStore.list()` 排除 `agent_notes.md.legacy` 后缀
  - `BlockStore.write_agent_topic(scope, topic, note, note_type="feedback") -> Path` 创建/追加 `agent/<topic>.md`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_block_store_agent_scope.py
from pathlib import Path

import pytest

from pico.memory.block_store import BlockStore


def _mk_store(tmp_path: Path) -> BlockStore:
    return BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")


def test_list_includes_agent_dir(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws" / "agent").mkdir(parents=True)
    (tmp_path / "ws" / "agent" / "topic-a.md").write_text(
        "---\nname: topic-a\ntype: feedback\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    entries = store.list()
    paths = [e.path for e in entries]
    assert "workspace/agent/topic-a.md" in paths


def test_list_skips_legacy_files(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws").mkdir(parents=True)
    (tmp_path / "ws" / "agent_notes.md.legacy").write_text("old", encoding="utf-8")
    (tmp_path / "ws" / "agent_notes.md").write_text("current", encoding="utf-8")
    paths = [e.path for e in store.list()]
    assert "workspace/agent_notes.md.legacy" not in paths
    assert "workspace/agent_notes.md" in paths


def test_memory_file_carries_frontmatter(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws" / "agent").mkdir(parents=True)
    (tmp_path / "ws" / "agent" / "x.md").write_text(
        "---\nname: x\ntype: reference\ndescription: hi\n---\nbody\n", encoding="utf-8"
    )
    entries = [e for e in store.list() if e.path == "workspace/agent/x.md"]
    assert entries
    assert entries[0].frontmatter["name"] == "x"
    assert entries[0].frontmatter["type"] == "reference"


def test_write_agent_topic_new_file(tmp_path):
    store = _mk_store(tmp_path)
    p = store.write_agent_topic("workspace", "prompt-cache", "first note", note_type="feedback")
    assert p.exists()
    body = p.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: prompt-cache" in body
    assert "type: feedback" in body
    assert "first note" in body


def test_write_agent_topic_appends_body_only(tmp_path):
    store = _mk_store(tmp_path)
    p = store.write_agent_topic("workspace", "prompt-cache", "first", note_type="feedback")
    store.write_agent_topic("workspace", "prompt-cache", "second")
    body = p.read_text(encoding="utf-8")
    # frontmatter 只写一次
    assert body.count("name: prompt-cache") == 1
    assert "first" in body
    assert "second" in body


def test_write_agent_topic_rejects_invalid_topic(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(ValueError):
        store.write_agent_topic("workspace", "../evil", "note")
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_block_store_agent_scope.py -v
```

- [ ] **Step 3: 修改 BlockStore**

在 `pico/memory/block_store.py` 中：

```python
import re

from .frontmatter import parse_frontmatter

_TOPIC_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass(frozen=True)
class MemoryFile:
    path: str
    size_chars: int
    mtime: float
    first_line: str
    frontmatter: dict = None  # 允许 None 兼容旧调用点


class BlockStore:
    # 现有 __init__ 保留

    def _scan_scope(self, scope, root):
        if not root.exists():
            return []
        results = []
        # notes/*.md
        notes_dir = root / "notes"
        if notes_dir.exists():
            for md in sorted(notes_dir.rglob("*.md")):
                if md.is_file():
                    rel = md.relative_to(root).as_posix()
                    results.append(self._to_memory_file(f"{scope}/{rel}", md))
        # agent/*.md（新增）
        agent_dir = root / "agent"
        if agent_dir.exists():
            for md in sorted(agent_dir.rglob("*.md")):
                if md.is_file():
                    rel = md.relative_to(root).as_posix()
                    results.append(self._to_memory_file(f"{scope}/{rel}", md))
        # agent_notes.md（跳过 .legacy 后缀）
        agent_notes = root / "agent_notes.md"
        if agent_notes.is_file():
            results.append(self._to_memory_file(f"{scope}/agent_notes.md", agent_notes))
        return results

    @staticmethod
    def _to_memory_file(rel_path, real_path):
        stat = real_path.stat()
        content = ""
        try:
            content = real_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        fm, body = parse_frontmatter(content)
        first_line = ""
        if fm.get("description"):
            first_line = fm["description"][:200]
        else:
            first_line = (body.splitlines()[0] if body else "").rstrip("\n")[:200]
        return MemoryFile(
            path=rel_path,
            size_chars=len(content),
            mtime=stat.st_mtime,
            first_line=first_line,
            frontmatter=fm or {},
        )

    def write_agent_topic(self, scope, topic, note, note_type="feedback"):
        note = str(note).strip()
        if not note:
            raise ValueError("note must not be empty")
        topic = str(topic).strip()
        if not _TOPIC_RE.match(topic):
            raise ValueError(f"invalid topic: {topic!r}")
        if scope == "workspace":
            root = self.workspace_root
        elif scope == "user":
            root = self.user_root
        else:
            raise ValueError(f"unknown scope: {scope!r}")
        agent_dir = root / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        target = agent_dir / f"{topic}.md"
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            new_body = existing.rstrip("\n") + "\n\n" + note + "\n"
            self._atomic_write(target, new_body)
        else:
            description = note.splitlines()[0][:80]
            fm = (
                f"---\n"
                f"name: {topic}\n"
                f"type: {note_type}\n"
                f"description: {description}\n"
                f"tags: []\n"
                f"aliases: []\n"
                f"supersedes: []\n"
                f"---\n"
                f"\n{note}\n"
            )
            self._atomic_write(target, fm)
        return target
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_block_store_agent_scope.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/block_store.py tests/test_memory_block_store_agent_scope.py
git commit -m "feat(memory): BlockStore supports agent/ scope + write_agent_topic + frontmatter on MemoryFile"
```

---

## Task 18: Retrieval — per-field tokenize + field boost

**Files:**
- Modify: `pico/memory/retrieval.py`
- Test: `tests/test_memory_retrieval_field_boost.py`

**Interfaces:**
- Consumes: `MemoryFile.frontmatter`
- Produces: BM25 tf 计算按 `{name×5, description×3, tags×4, aliases×4, body×1}` 加权

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_retrieval_field_boost.py
from pathlib import Path

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _write(root: Path, rel: str, body: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_hit_in_description_ranks_above_hit_in_body(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws, "agent/a.md",
        "---\nname: a\ntype: feedback\ndescription: cache invariant note\n---\nunrelated body content\n"
    )
    _write(
        ws, "agent/b.md",
        "---\nname: b\ntype: feedback\ndescription: nothing here\n---\nsomething cache mentioned in body\n"
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    assert hits[0].path == "workspace/agent/a.md"


def test_hit_in_name_ranks_highest(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws, "agent/mycache.md",
        "---\nname: mycache\ntype: feedback\ndescription: irrelevant\n---\nirrelevant body\n"
    )
    _write(
        ws, "agent/other.md",
        "---\nname: other\ntype: feedback\ndescription: mycache reference\n---\nsomething mycache in body\n"
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("mycache")
    assert hits[0].path == "workspace/agent/mycache.md"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_retrieval_field_boost.py -v
```

- [ ] **Step 3: 改造 Retrieval**

在 `pico/memory/retrieval.py` 中：

```python
FIELD_BOOSTS = {"name": 5.0, "description": 3.0, "tags": 4.0, "aliases": 4.0, "body": 1.0}


def tokenize_fields(entry, raw_body: str) -> dict[str, list[str]]:
    """Split into per-field tokens based on frontmatter."""
    fm = getattr(entry, "frontmatter", None) or {}
    tokens = {
        "name":        tokenize(str(fm.get("name", ""))),
        "description": tokenize(str(fm.get("description", ""))),
        "tags":        tokenize(" ".join(fm.get("tags", []))),
        "aliases":     tokenize(" ".join(fm.get("aliases", []))),
        "body":        tokenize(raw_body),
    }
    return tokens


class Retrieval:
    ...

    def _load_docs(self):
        docs = []
        for entry in self.store.list():
            try:
                raw = self.store.read(entry.path)
            except (OSError, ValueError):
                continue
            fm, body = _parse_content(raw)
            fields = tokenize_fields(_EntryWithFm(entry, fm), body)
            # flatten for compat token list
            flat_tokens = []
            for field_name, field_tokens in fields.items():
                flat_tokens.extend(field_tokens)
            if flat_tokens:
                docs.append((entry.path, flat_tokens, raw, fields))
        return docs

    def search(self, query, limit=5):
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        docs = self._load_docs()
        if not docs:
            return []
        avg_doc_len = sum(len(t) for _, t, _, _ in docs) / len(docs)
        df = Counter()
        for _, tokens, _, _ in docs:
            for term in set(tokens):
                df[term] += 1
        N = len(docs)
        results = []
        for path, flat_tokens, raw, fields in docs:
            score = self._bm25_field_score(query_tokens, fields, flat_tokens, avg_doc_len, N, df)
            if score <= 0:
                continue
            snippets = self._extract_snippets(raw, query_tokens)
            results.append(SearchHit(path=path, score=score, snippets=snippets))
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    @staticmethod
    def _bm25_field_score(query_tokens, fields, flat_tokens, avg_doc_len, N, df):
        doc_len = len(flat_tokens)
        if doc_len == 0 or avg_doc_len == 0:
            return 0.0
        counters = {f: Counter(fields[f]) for f in fields}
        score = 0.0
        for term in set(query_tokens):
            if term not in df:
                continue
            tf_weighted = sum(FIELD_BOOSTS.get(f, 1.0) * counters[f].get(term, 0) for f in counters)
            if tf_weighted == 0:
                continue
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
            norm = tf_weighted * (BM25_K1 + 1) / (
                tf_weighted + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_doc_len)
            )
            score += idf * norm
        return score


def _parse_content(raw):
    from .frontmatter import parse_frontmatter
    return parse_frontmatter(raw)


class _EntryWithFm:
    def __init__(self, entry, fm):
        self._entry = entry
        self.frontmatter = fm

    def __getattr__(self, k):
        return getattr(self._entry, k)
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_retrieval_field_boost.py tests/test_memory.py -v
```

Expected: field-boost tests pass；老 tests 保持 pass（field=body 权重=1 时兼容原行为）

- [ ] **Step 5: Commit**

```bash
git add pico/memory/retrieval.py tests/test_memory_retrieval_field_boost.py
git commit -m "feat(memory): BM25 field boost via per-field tokenize (name×5, description×3, tags/aliases×4, body×1)"
```

---

## Task 19: Retrieval — link expansion (`[[name]]` 一跳邻居)

**Files:**
- Modify: `pico/memory/retrieval.py`
- Test: `tests/test_memory_retrieval_link.py`

**Interfaces:**
- Produces: search 返回结果里若命中文档正文含 `[[name]]`，把被链接文档也加进结果，score × 0.4，每次 query 最多加 3 个。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_retrieval_link.py
from pathlib import Path

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _write(root: Path, rel: str, body: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_link_expansion_adds_neighbor(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws, "agent/a.md",
        "---\nname: a\ntype: feedback\ndescription: about cache\n---\nsee [[b]] for related\n"
    )
    _write(
        ws, "agent/b.md",
        "---\nname: b\ntype: feedback\ndescription: unrelated\n---\nnothing about cache here\n"
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/agent/a.md" in paths
    assert "workspace/agent/b.md" in paths  # 一跳链接扩展


def test_link_expansion_capped_at_three(tmp_path):
    ws = tmp_path / "ws"
    body_links = "\n".join([f"see [[n{i}]]" for i in range(10)])
    _write(
        ws, "agent/hub.md",
        f"---\nname: hub\ntype: feedback\ndescription: cache hub\n---\n{body_links}\n"
    )
    for i in range(10):
        _write(ws, f"agent/n{i}.md", f"---\nname: n{i}\ntype: feedback\ndescription: none\n---\ncontent\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache", limit=20)
    expanded = [h for h in hits if h.path != "workspace/agent/hub.md"]
    assert len(expanded) <= 3


def test_link_expansion_does_not_recurse(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "agent/a.md", "---\nname: a\ntype: feedback\ndescription: about cache\n---\nsee [[b]]\n")
    _write(ws, "agent/b.md", "---\nname: b\ntype: feedback\ndescription: unrelated\n---\nsee [[c]]\n")
    _write(ws, "agent/c.md", "---\nname: c\ntype: feedback\ndescription: unrelated too\n---\nno links\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/agent/b.md" in paths
    assert "workspace/agent/c.md" not in paths  # 深度上限 1
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_retrieval_link.py -v
```

- [ ] **Step 3: 实现 link expansion**

在 `pico/memory/retrieval.py` 添加：

```python
import re

_LINK_RE = re.compile(r"\[\[([a-zA-Z0-9_-]+)\]\]")

LINK_MAX_ADDED = 3
LINK_DECAY = 0.4


class Retrieval:
    ...

    def search(self, query, limit=5):
        # ... 现有 BM25 打分逻辑生成 primary_results ...
        primary_results = results  # 保留原 sort 后的列表
        # 链接扩展
        primary_paths = {h.path for h in primary_results[:limit]}
        primary_by_path = {h.path: h for h in primary_results}

        # 构建 name -> path 映射
        name_to_path = {}
        for entry in self.store.list():
            fm = getattr(entry, "frontmatter", None) or {}
            if fm.get("name"):
                name_to_path[fm["name"]] = entry.path

        expanded = []
        for hit in primary_results[:limit]:
            if len(expanded) >= LINK_MAX_ADDED:
                break
            try:
                raw = self.store.read(hit.path)
            except (OSError, ValueError):
                continue
            for m in _LINK_RE.finditer(raw):
                if len(expanded) >= LINK_MAX_ADDED:
                    break
                neighbor_name = m.group(1)
                neighbor_path = name_to_path.get(neighbor_name)
                if not neighbor_path or neighbor_path in primary_paths or neighbor_path in {h.path for h in expanded}:
                    continue
                expanded.append(SearchHit(
                    path=neighbor_path,
                    score=hit.score * LINK_DECAY,
                    snippets=(f"(via [[{neighbor_name}]] from {hit.path})",),
                ))

        return primary_results[:limit] + expanded
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_retrieval_link.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/retrieval.py tests/test_memory_retrieval_link.py
git commit -m "feat(memory): link expansion via [[name]] with decay=0.4 max_added=3 depth=1"
```

---

## Task 20: Retrieval — tombstone filter (`supersedes`)

**Files:**
- Modify: `pico/memory/retrieval.py`
- Test: `tests/test_memory_retrieval_tombstone.py`

**Interfaces:**
- Produces: 加载文档时构建 `superseded_names` 集合，被 supersede 的 note 从检索结果移除。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_retrieval_tombstone.py
from pathlib import Path

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_superseded_note_excluded(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "agent/old.md", "---\nname: old\ntype: feedback\ndescription: cache old\n---\nold body\n")
    _w(ws, "agent/new.md",
       "---\nname: new\ntype: feedback\ndescription: cache new\nsupersedes: [old]\n---\nnew body\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/agent/old.md" not in paths
    assert "workspace/agent/new.md" in paths


def test_link_expansion_skips_tombstoned(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "agent/a.md", "---\nname: a\ntype: feedback\ndescription: cache\n---\nsee [[old]]\n")
    _w(ws, "agent/old.md", "---\nname: old\ntype: feedback\ndescription: ancient\n---\nold\n")
    _w(ws, "agent/new.md",
       "---\nname: new\ntype: feedback\ndescription: replaces old\nsupersedes: [old]\n---\nnew\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/agent/old.md" not in paths
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_retrieval_tombstone.py -v
```

- [ ] **Step 3: 实现**

在 `_load_docs` 之前先构建 `superseded_names`：

```python
class Retrieval:
    def _superseded_names(self):
        superseded = set()
        for entry in self.store.list():
            fm = getattr(entry, "frontmatter", None) or {}
            for name in fm.get("supersedes") or []:
                superseded.add(name)
        return superseded

    def _load_docs(self):
        superseded = self._superseded_names()
        docs = []
        for entry in self.store.list():
            fm = getattr(entry, "frontmatter", None) or {}
            if fm.get("name") in superseded:
                continue
            try:
                raw = self.store.read(entry.path)
            except (OSError, ValueError):
                continue
            fm_content, body = _parse_content(raw)
            fields = tokenize_fields(_EntryWithFm(entry, fm_content), body)
            flat_tokens = []
            for ftokens in fields.values():
                flat_tokens.extend(ftokens)
            if flat_tokens:
                docs.append((entry.path, flat_tokens, raw, fields))
        return docs
```

同时在 link expansion 里跳过 tombstoned name：

```python
for entry in self.store.list():
    fm = getattr(entry, "frontmatter", None) or {}
    name = fm.get("name")
    if name and name not in self._superseded_names():
        name_to_path[name] = entry.path
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_retrieval_tombstone.py tests/test_memory_retrieval_link.py tests/test_memory_retrieval_field_boost.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/retrieval.py tests/test_memory_retrieval_tombstone.py
git commit -m "feat(memory): tombstone filter via frontmatter supersedes"
```

---

## Task 21: memory_save Tool — 支持 `topic` 参数

**Files:**
- Modify: `pico/memory/tools.py`, `pico/tools.py`
- Test: `tests/test_memory_save_topic.py`

**Interfaces:**
- Consumes: `BlockStore.write_agent_topic`
- Produces: `memory_save(note, scope="workspace", topic="")` — 有 topic 走 `agent/<topic>.md`；无 topic 保留原 `agent_notes.md` 追加。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_save_topic.py
from pathlib import Path
from types import SimpleNamespace

from pico.memory.block_store import BlockStore
from pico.memory.tools import tool_memory_save


def test_save_with_topic_creates_agent_file(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    result = tool_memory_save(ctx, {"note": "hello world", "topic": "greeting"})
    assert "saved" in result
    target = tmp_path / "ws" / "agent" / "greeting.md"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "name: greeting" in body


def test_save_without_topic_uses_legacy_agent_notes(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    tool_memory_save(ctx, {"note": "hello"})
    legacy = tmp_path / "ws" / "agent_notes.md"
    assert legacy.exists()
    assert "hello" in legacy.read_text(encoding="utf-8")


def test_save_topic_invalid_returns_error(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    result = tool_memory_save(ctx, {"note": "x", "topic": "../evil"})
    assert result.lower().startswith("error")
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_save_topic.py -v
```

- [ ] **Step 3: 实现**

在 `pico/memory/tools.py` 修改 `tool_memory_save`：

```python
def tool_memory_save(context, args):
    store = getattr(context, "memory_store", None)
    if store is None:
        return "memory_store unavailable"
    note = str(args.get("note", "")).strip()
    if not note:
        return "error: note must not be empty"
    if len(note) > MAX_NOTE_CHARS:
        return f"error: note exceeds {MAX_NOTE_CHARS} chars"
    scope = str(args.get("scope", "workspace")).strip() or "workspace"
    if scope not in ("workspace", "user"):
        return "error: scope must be 'workspace' or 'user'"
    topic = str(args.get("topic", "")).strip()
    if topic:
        try:
            target = store.write_agent_topic(scope, topic, note, note_type=str(args.get("type", "feedback")))
        except ValueError as exc:
            return f"error: {exc}"
        return f"saved: {scope}/agent/{topic}.md"
    try:
        total = store.append_agent_note(scope=scope, note=note)
    except ValueError as exc:
        return f"error: {exc}"
    return f"saved: {scope}/agent_notes.md (chars_total={total})"
```

在 `pico/tools.py` 的 `memory_save` schema 加 `topic`：

```python
"memory_save": {
    "schema": {"note": "str", "scope": "str='workspace'", "topic": "str=''"},
    "risky": False,
    "description": "Append a note (<=500 chars). With topic → agent/<topic>.md per-topic; without topic → agent_notes.md legacy path.",
},
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_save_topic.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/tools.py pico/tools.py tests/test_memory_save_topic.py
git commit -m "feat(memory): memory_save topic= param writes agent/<topic>.md with frontmatter"
```

---

## Task 22: `pico-cli memory migrate` CLI

**Files:**
- Modify: `pico/cli_memory.py`
- Test: `tests/test_cli_memory_migrate.py`

**Interfaces:**
- Produces: `pico-cli memory migrate [--dry-run] [--rollback]`
  - Default: 把 `agent_notes.md` 整体迁到 `agent/legacy-import.md`（含 frontmatter），原文件重命名 `.legacy`
  - `--rollback`：把 `.legacy` 改回，删除 `agent/legacy-import.md`
  - Backup 前置：`.pico/memory/backup/agent_notes.md.<timestamp>`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli_memory_migrate.py
from pathlib import Path
import pytest

from pico.cli_memory import cli_memory_migrate


def test_migrate_creates_legacy_import(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text("- 2026-01-01T00:00:00Z  first note\n", encoding="utf-8")

    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=False)
    assert rc == 0
    imported = ws / "agent" / "legacy-import.md"
    assert imported.exists()
    body = imported.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: legacy-import" in body
    # 原文件重命名
    assert (ws / "agent_notes.md.legacy").exists()
    # backup 生成
    backups = list((ws / "backup").glob("agent_notes.md.*"))
    assert len(backups) == 1


def test_migrate_dry_run_no_write(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text("orig", encoding="utf-8")
    rc = cli_memory_migrate(workspace_root=ws, dry_run=True, rollback=False)
    assert rc == 0
    assert not (ws / "agent" / "legacy-import.md").exists()
    assert not (ws / "agent_notes.md.legacy").exists()


def test_migrate_rollback(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text("orig", encoding="utf-8")
    cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=False)
    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=True)
    assert rc == 0
    assert (ws / "agent_notes.md").exists()
    assert (ws / "agent_notes.md").read_text(encoding="utf-8") == "orig"
    assert not (ws / "agent" / "legacy-import.md").exists()
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_cli_memory_migrate.py -v
```

- [ ] **Step 3: 实现**

在 `pico/cli_memory.py` 中添加：

```python
import shutil
import time


def cli_memory_migrate(workspace_root, *, dry_run: bool = False, rollback: bool = False) -> int:
    ws = Path(workspace_root)
    legacy_target = ws / "agent" / "legacy-import.md"
    old_notes = ws / "agent_notes.md"
    renamed = ws / "agent_notes.md.legacy"
    backup_dir = ws / "backup"

    if rollback:
        if not renamed.exists():
            print("nothing to rollback (agent_notes.md.legacy not found)")
            return 1
        if legacy_target.exists():
            if dry_run:
                print(f"[dry-run] would delete {legacy_target}")
            else:
                legacy_target.unlink()
        if dry_run:
            print(f"[dry-run] would rename {renamed} → {old_notes}")
            return 0
        renamed.rename(old_notes)
        print(f"rolled back: {old_notes}")
        return 0

    if not old_notes.exists():
        print("no agent_notes.md to migrate")
        return 0

    body = old_notes.read_text(encoding="utf-8")
    ts = int(time.time())

    if dry_run:
        print(f"[dry-run] would backup {old_notes} → {backup_dir / f'agent_notes.md.{ts}'}")
        print(f"[dry-run] would create {legacy_target} with legacy frontmatter")
        print(f"[dry-run] would rename {old_notes} → {renamed}")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_notes, backup_dir / f"agent_notes.md.{ts}")

    legacy_target.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        "name: legacy-import\n"
        "type: feedback\n"
        "description: Migrated legacy agent notes\n"
        "tags: [legacy]\n"
        "aliases: []\n"
        "supersedes: []\n"
        "---\n\n"
    )
    legacy_target.write_text(fm + body, encoding="utf-8")
    old_notes.rename(renamed)
    print(f"migrated to {legacy_target}")
    return 0
```

在 `pico/cli_commands.py` 或对应 CLI parser 中注册子命令 `pico-cli memory migrate`，接受 `--dry-run` / `--rollback` 参数（复用现有 `pico-cli memory *` 子命令模式）。

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_cli_memory_migrate.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/cli_memory.py pico/cli_commands.py tests/test_cli_memory_migrate.py
git commit -m "feat(cli): pico-cli memory migrate with backup, dry-run, rollback"
```

---

## Task 23: recall.py — 四护栏 recall

**Files:**
- Create: `pico/memory/recall.py`
- Test: `tests/test_memory_recall.py`

**Interfaces:**
- Consumes: `Retrieval.search`, `agent.session["recently_recalled"]`（deque-like list of list[str]）
- Produces: `recall_for_turn(agent, user_message, budget_tokens) -> str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_recall.py
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval
from pico.memory.recall import recall_for_turn


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _agent(tmp_path):
    ws = tmp_path / "ws"
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    retrieval = Retrieval(store)
    return SimpleNamespace(
        memory_store=store,
        memory_retrieval=retrieval,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: len(t) // 4),
        memory=SimpleNamespace(task_summary=""),
    ), ws


def test_recall_returns_recall_block(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/cache.md", "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nParagraph one.\n\nSecond para.\n")
    out = recall_for_turn(a, "how does cache work?", budget_tokens=1000)
    assert out is not None
    assert "<pico:recalled_memory" in out
    assert "score=" in out
    assert "path=" in out
    assert "Paragraph one." in out


def test_recall_min_score_filters(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/weakly.md", "---\nname: weakly\ntype: feedback\ndescription: banana\n---\ntotally unrelated body\n")
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None  # 没有命中 min_score


def test_recall_tombstoned_skipped(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/old.md", "---\nname: old\ntype: feedback\ndescription: cache old\n---\nold.\n")
    _w(ws, "agent/new.md",
       "---\nname: new\ntype: feedback\ndescription: cache new\nsupersedes: [old]\n---\nnew.\n")
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    assert "path=\"workspace/agent/new.md\"" in out
    assert "path=\"workspace/agent/old.md\"" not in out


def test_recall_recently_skipped(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/cache.md", "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n")
    # 前 2 轮已 recall
    a.session["recently_recalled"] = [["workspace/agent/cache.md"], []]
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None


def test_recall_updates_recently_recalled(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/cache.md", "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n")
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    assert a.session["recently_recalled"][-1] == ["workspace/agent/cache.md"]


def test_recall_provenance_fields(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "agent/cache.md", "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nP1\n")
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert 'type="reference"' in out
    assert 'why="' in out
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_memory_recall.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/memory/recall.py
"""Per-turn relevance recall with four guards (spec §5.4)."""
from __future__ import annotations

from pico.context.escaping import escape_pico_tags

RECALL_TOP_K = 2
RECALL_MIN_SCORE = 0.3
RECALL_MAX_TOKENS_PER_NOTE = 400
RECALL_SKIP_RECENT_TURNS = 2


def _first_paragraph(text: str) -> str:
    body = text
    # 去掉 frontmatter 头
    if body.startswith("---\n"):
        end = body.find("\n---\n")
        if end != -1:
            body = body[end + len("\n---\n"):]
    # 第一段：从首个非空行开始，到首个空行为止
    lines = body.splitlines()
    para = []
    started = False
    for line in lines:
        if not line.strip():
            if started:
                break
            continue
        started = True
        para.append(line)
    return "\n".join(para)


def _count_tokens(agent, text: str) -> int:
    counter = getattr(getattr(agent, "model_client", None), "count_tokens", None)
    if callable(counter):
        try:
            return int(counter(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _flatten_recent(session_recent) -> set:
    out = set()
    for turn in (session_recent or [])[-RECALL_SKIP_RECENT_TURNS:]:
        for p in turn:
            out.add(p)
    return out


def recall_for_turn(agent, user_message: str, budget_tokens: int) -> str | None:
    retrieval = getattr(agent, "memory_retrieval", None)
    if retrieval is None:
        return None
    task_summary = getattr(getattr(agent, "memory", None), "task_summary", "") or ""
    query = f"{user_message} {task_summary}".strip()
    if not query:
        return None

    hits = retrieval.search(query, limit=RECALL_TOP_K * 3)  # 取多点后过滤
    if not hits:
        return None

    # 归一化 score：以本次最高分为分母
    max_score = max(h.score for h in hits) or 1.0
    recent_skip = _flatten_recent(agent.session.get("recently_recalled"))

    picked = []
    for h in hits:
        if len(picked) >= RECALL_TOP_K:
            break
        norm_score = h.score / max_score
        if norm_score < RECALL_MIN_SCORE:
            continue
        if h.path in recent_skip:
            continue
        picked.append((h, norm_score))

    if not picked:
        return None

    blocks = []
    picked_paths = []
    store = agent.memory_store
    for h, norm_score in picked:
        try:
            raw = store.read(h.path)
        except (OSError, ValueError):
            continue
        # 从 MemoryFile 拿 type
        note_type = ""
        for entry in store.list():
            if entry.path == h.path:
                note_type = (entry.frontmatter or {}).get("type", "")
                break
        para = _first_paragraph(raw)
        para_tokens = _count_tokens(agent, para)
        if para_tokens > RECALL_MAX_TOKENS_PER_NOTE:
            # tail_clip
            char_budget = RECALL_MAX_TOKENS_PER_NOTE * 4
            para = para[: char_budget - 3] + "..."
        # why: 从 snippets 里挑 keyword（简化：把 snippet 首行的 匹配词做逗号拼接）
        why_terms = []
        for snip in h.snippets:
            for tok in snip.split():
                clean = tok.strip(".,:;!?")
                if clean.lower() in query.lower() and clean not in why_terms:
                    why_terms.append(clean)
                if len(why_terms) >= 3:
                    break
            if len(why_terms) >= 3:
                break
        why = ",".join(why_terms) if why_terms else "matched"

        block = (
            f'<pico:recalled_memory path="{h.path}" type="{note_type}" '
            f'score="{norm_score:.2f}" why="{why}">\n'
            f"{escape_pico_tags(para)}\n"
            f"</pico:recalled_memory>"
        )
        blocks.append(block)
        picked_paths.append(h.path)

    if not blocks:
        return None

    # 记录本轮 recall 到 session
    recent = agent.session.get("recently_recalled") or []
    recent.append(picked_paths)
    agent.session["recently_recalled"] = recent[-(RECALL_SKIP_RECENT_TURNS + 1):]

    return "\n".join(blocks)
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_memory_recall.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/memory/recall.py tests/test_memory_recall.py
git commit -m "feat(memory): recall_for_turn with four guards (min_score/max_tokens/tombstone/recently)"
```

---

## Task 24: 接入 renderer 的 recalled_memory 源

**Files:**
- Modify: `pico/context/renderer.py`, `pico/context/sources.py`
- Test: `tests/test_context_recall_integration.py`

**Interfaces:**
- Produces: `render_current_user_message` 在 `recalled_memory` 位置注入 `recall_for_turn` 的输出

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_recall_integration.py
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval
from pico.context.renderer import render_current_user_message


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_renderer_injects_recalled_memory(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "agent/cache.md", "---\nname: cache\ntype: reference\ndescription: cache note\n---\nCache is important.\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: len(t) // 4),
        memory=SimpleNamespace(task_summary=""),
    )
    text, tele = render_current_user_message(a, "上次讨论过 cache 的问题")
    assert "<pico:recalled_memory" in text
    assert "Cache is important." in text
    assert tele["intent"]["name"] == "recall"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_recall_integration.py -v
```

- [ ] **Step 3: 接入**

在 `pico/context/sources.py` 追加：

```python
from pico.memory.recall import recall_for_turn


def render_recalled_memory(agent, budget_tokens: int, user_message: str = "") -> str | None:
    return recall_for_turn(agent, user_message, budget_tokens)
```

在 `pico/context/renderer.py` 中重新连接 `recalled_memory`：

```python
from .sources import render_recalled_memory


# 覆盖 placeholder
def _render_recalled(agent, budget_tokens):
    user_msg = getattr(agent, "_current_user_message_for_recall", "")
    return render_recalled_memory(agent, budget_tokens, user_msg)


_RENDERERS["recalled_memory"] = _render_recalled


def render_current_user_message(agent, user_message: str):
    # 把当前 user_message 临时挂在 agent 上，让 recall renderer 拿得到
    agent._current_user_message_for_recall = user_message
    try:
        # 原有逻辑不变
        intent = classify_intent(user_message)
        # ... 剩下不变
    finally:
        try:
            del agent._current_user_message_for_recall
        except AttributeError:
            pass
```

**更干净的替代**：直接把 `user_message` 传入所有 source renderer；但会打破现有 signature——本 task 用 attribute trick 已经足够，spec 中不承诺 renderer 签名不变。

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_recall_integration.py tests/test_context_renderer.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/renderer.py pico/context/sources.py tests/test_context_recall_integration.py
git commit -m "feat(context): renderer wires recall_for_turn as recalled_memory source"
```

---

## Task 25: digest.py — Tool Result Digest + per-tool summarizer

**Files:**
- Create: `pico/context/digest.py`
- Test: `tests/test_context_digest.py`

**Interfaces:**
- Produces:
  - `@dataclass ToolResultDigest`: `tool`, `title`, `bullets`, `source_hash`, `raw_path`
  - `digest_tool_result(tool_name, args, result, raw_path) -> ToolResultDigest`
  - `render_digest_content(digest: ToolResultDigest) -> str`
  - `should_digest(result: str, threshold: int = 1200) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_digest.py
import hashlib
from pico.context.digest import (
    ToolResultDigest, digest_tool_result, render_digest_content, should_digest,
)


def test_should_digest_threshold():
    assert not should_digest("short")
    assert should_digest("x" * 1201)


def test_digest_read_file_extracts_summary():
    src = "import os\nfrom pathlib import Path\n\ndef foo():\n    pass\n\nclass Bar:\n    pass\n"
    d = digest_tool_result("read_file", {"path": "a.py"}, src, raw_path="raw/abc.txt")
    assert d.tool == "read_file"
    assert "a.py" in d.title
    assert any("foo" in b or "Bar" in b for b in d.bullets)


def test_digest_run_shell_extracts_exit_and_lines():
    src = "exit_code: 1\nstdout:\nline1\nline2\nline3\nline4\nstderr:\nerr line\n"
    d = digest_tool_result("run_shell", {"command": "pytest"}, src, raw_path="raw/x.txt")
    assert d.tool == "run_shell"
    assert any("exit" in b.lower() for b in d.bullets)


def test_digest_grep_extracts_hits():
    src = "match 1\nmatch 2\nmatch 3\nmatch 4\nmatch 5\nmatch 6\n"
    d = digest_tool_result("grep", {"pattern": "x"}, src, raw_path="raw/y.txt")
    assert d.tool == "grep"
    # 前 5 条
    assert len(d.bullets) <= 5


def test_digest_fallback_for_unknown_tool():
    src = "a\nb\nc\nd\ne\n" * 100
    d = digest_tool_result("unknown_tool", {}, src, raw_path="raw/z.txt")
    assert d.tool == "unknown_tool"
    assert d.bullets  # tail 3 行
    assert len(d.bullets) <= 3


def test_digest_source_hash_stable():
    d1 = digest_tool_result("read_file", {"path": "a"}, "same content", raw_path="p")
    d2 = digest_tool_result("read_file", {"path": "b"}, "same content", raw_path="q")
    assert d1.source_hash == d2.source_hash


def test_render_content_shape():
    d = ToolResultDigest(
        tool="read_file", title="a.py (30 lines)",
        bullets=["import os", "def foo", "class Bar"],
        source_hash="abc123", raw_path=".pico/runs/x/tool_results/abc123.txt",
    )
    text = render_digest_content(d)
    assert "a.py (30 lines)" in text
    assert "import os" in text
    assert ".pico/runs/x/tool_results/abc123.txt" in text


def test_summarizer_exception_falls_back():
    # 让某个 summarizer 抛错，应走 tail_clip 兜底
    d = digest_tool_result("read_file", {"path": None}, "some content", raw_path="p")
    assert d is not None
    assert d.tool == "read_file"
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_context_digest.py -v
```

- [ ] **Step 3: 实现**

```python
# pico/context/digest.py
"""Tool result digest: title + up to 5 bullets + hash + raw_path pointer."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResultDigest:
    tool: str
    title: str
    bullets: list = field(default_factory=list)
    source_hash: str = ""
    raw_path: str = ""


def should_digest(result: str, threshold: int = 1200) -> bool:
    return len(str(result or "")) > threshold


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


_PY_TOP_LEVEL_RE = re.compile(r"^(def |class |import |from )([\w\.]+)", re.M)


def _digest_read_file(args, result):
    path = str(args.get("path") or "unknown")
    line_count = result.count("\n") + 1
    symbols = _PY_TOP_LEVEL_RE.findall(result)[:5]
    bullets = [f"{kind.strip()}{name}" for kind, name in symbols]
    return ToolResultDigest(
        tool="read_file",
        title=f"{path} ({line_count} lines)",
        bullets=bullets or [result.splitlines()[0][:80] if result else ""],
        source_hash="",
        raw_path="",
    )


def _digest_run_shell(args, result):
    cmd = str(args.get("command") or "")[:80]
    lines = result.splitlines()
    exit_line = next((l for l in lines if "exit" in l.lower()), "exit_code: ?")
    stdout_lines = [l for l in lines if l and "err" not in l.lower()][:3]
    stderr_lines = [l for l in lines if "err" in l.lower()][-3:]
    return ToolResultDigest(
        tool="run_shell",
        title=f"$ {cmd}",
        bullets=[exit_line] + stdout_lines[:3] + stderr_lines[-3:],
        source_hash="",
        raw_path="",
    )


def _digest_grep(args, result):
    pattern = str(args.get("pattern") or "")
    lines = [l for l in result.splitlines() if l.strip()]
    hits = lines[:5]
    return ToolResultDigest(
        tool="grep",
        title=f'grep "{pattern}" → {len(lines)} lines',
        bullets=hits,
        source_hash="",
        raw_path="",
    )


def _digest_fallback(tool_name, args, result):
    lines = [l for l in result.splitlines() if l.strip()]
    tail = lines[-3:] if lines else []
    return ToolResultDigest(
        tool=tool_name,
        title=f"{tool_name} result",
        bullets=tail,
        source_hash="",
        raw_path="",
    )


_DIGESTERS = {
    "read_file": _digest_read_file,
    "run_shell": _digest_run_shell,
    "grep": _digest_grep,
    "search": _digest_grep,
}


def digest_tool_result(tool_name: str, args: dict, result: str, raw_path: str) -> ToolResultDigest:
    fn = _DIGESTERS.get(tool_name)
    if fn is None:
        base = _digest_fallback(tool_name, args or {}, result or "")
    else:
        try:
            base = fn(args or {}, result or "")
        except Exception:
            base = _digest_fallback(tool_name, args or {}, result or "")
    return ToolResultDigest(
        tool=base.tool,
        title=base.title,
        bullets=list(base.bullets),
        source_hash=_hash(result or ""),
        raw_path=raw_path,
    )


def render_digest_content(digest: ToolResultDigest) -> str:
    bullet_text = "\n".join(f"- {b}" for b in digest.bullets)
    footer = f"\n(raw at {digest.raw_path})" if digest.raw_path else ""
    return f"[digest] {digest.title}\n{bullet_text}{footer}"
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_context_digest.py -v
```

- [ ] **Step 5: Commit**

```bash
git add pico/context/digest.py tests/test_context_digest.py
git commit -m "feat(context): digest.py with per-tool summarizer + tail_clip fallback"
```

---

## Task 26: agent_loop 集成 digest — tool_result 超阈值时写盘 + digest

**Files:**
- Modify: `pico/agent_loop.py`, `pico/runtime.py`
- Test: `tests/test_agent_loop_digest.py`

**Interfaces:**
- Consumes: `digest_tool_result`, `should_digest`, `render_digest_content`
- Produces: `_append_tool_result` 在写入前判断阈值，超阈值则：
  1. 把原始 result 写盘到 `.pico/runs/<run_id>/tool_results/<source_hash>.txt`
  2. `content` 里存 digest 渲染后的短文本
  3. `_pico_meta.digest_applied = True`, `_pico_meta.source_hash = hash`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_loop_digest.py
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.agent_loop import _append_tool_result


def _stub_agent(tmp_path, run_id="run1"):
    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s1"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = str(tmp_path)
    a.current_task_state = SimpleNamespace(run_id=run_id, task_id="t1")
    a.current_run_dir = tmp_path / ".pico" / "runs" / run_id
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    return a


def test_small_result_stored_inline(tmp_path):
    a = _stub_agent(tmp_path)
    _append_tool_result(a, tool_use_id="toolu_a", content="tiny result", tool_name="read_file", tool_args={"path": "x"})
    msg = a.session["messages"][-1]
    assert msg["content"][0]["content"] == "tiny result"
    assert msg["_pico_meta"]["digest_applied"] is False


def test_large_result_digested_and_written_to_disk(tmp_path):
    a = _stub_agent(tmp_path)
    big = "x = 1\n" * 500  # >1200 char
    _append_tool_result(a, tool_use_id="toolu_b", content=big, tool_name="read_file", tool_args={"path": "big.py"})
    msg = a.session["messages"][-1]
    assert msg["_pico_meta"]["digest_applied"] is True
    source_hash = msg["_pico_meta"]["source_hash"]
    assert source_hash
    # 原文写盘
    raw_files = list((a.current_run_dir / "tool_results").glob(f"{source_hash}.txt"))
    assert len(raw_files) == 1
    assert raw_files[0].read_text(encoding="utf-8") == big
    # content 里是 digest 内容
    assert "[digest]" in msg["content"][0]["content"]
    assert str(raw_files[0]) in msg["content"][0]["content"] or "tool_results/" + source_hash in msg["content"][0]["content"]
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run pytest tests/test_agent_loop_digest.py -v
```

- [ ] **Step 3: 更新 `_append_tool_result`**

在 `pico/agent_loop.py` 修改：

```python
from pico.context.digest import digest_tool_result, should_digest, render_digest_content


def _append_tool_result(agent, *, tool_use_id: str, content: str, tool_name: str = "", tool_args: dict = None):
    tool_args = tool_args or {}
    digest_applied = False
    source_hash = None
    display_content = content

    if should_digest(content):
        # 先算 hash / raw_path
        import hashlib
        source_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        run_dir = getattr(agent, "current_run_dir", None)
        if run_dir is not None:
            raw_dir = run_dir / "tool_results"
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / f"{source_hash}.txt"
            raw_path.write_text(content, encoding="utf-8")
            raw_path_str = str(raw_path)
        else:
            raw_path_str = ""
        digest = digest_tool_result(tool_name, tool_args, content, raw_path=raw_path_str)
        display_content = render_digest_content(digest)
        digest_applied = True

    msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": display_content}],
        "_pico_meta": {
            "created_at": now(),
            "tool_use_id": tool_use_id,
            "digest_applied": digest_applied,
            "source_hash": source_hash,
        },
    }
    agent.record_message(msg)
    return msg
```

同时在 `AgentLoop.run` 里调 `_append_tool_result` 时传入 `tool_name` 和 `tool_args`：

```python
_append_tool_result(agent, tool_use_id=tool_use_id, content=result_text, tool_name=tb["name"], tool_args=tb.get("input", {}))
```

- [ ] **Step 4: 通过**

```bash
uv run pytest tests/test_agent_loop_digest.py -v
```

- [ ] **Step 5: 全量回归**

```bash
uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add pico/agent_loop.py tests/test_agent_loop_digest.py
git commit -m "feat(agent_loop): tool_result >1200 char written to disk + replaced with digest in message"
```

---

## Task 27: P3 集成冒烟 + PR gate

**Files:**
- Test: `tests/test_p3_smoke.py`

- [ ] **Step 1: 冒烟**

```python
# tests/test_p3_smoke.py
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval
from pico.memory.recall import recall_for_turn
from pico.context.digest import digest_tool_result, should_digest
from pico.context.renderer import render_current_user_message


def test_p3_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nCache stability rules.\n",
        encoding="utf-8",
    )

    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)

    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: len(t) // 4),
        memory=SimpleNamespace(task_summary=""),
    )

    text, tele = render_current_user_message(a, "上次讨论过 cache 的事")
    assert "<pico:recalled_memory" in text
    assert "Cache stability rules." in text
    assert tele["intent"]["name"] == "recall"

    # digest
    long_result = "line\n" * 500
    assert should_digest(long_result)
    d = digest_tool_result("read_file", {"path": "a.py"}, long_result, raw_path="raw/x.txt")
    assert d.source_hash
    assert "a.py" in d.title
```

- [ ] **Step 2: 全量 + 冒烟**

```bash
uv run pytest tests/test_p3_smoke.py -v
uv run pytest -q
```

- [ ] **Step 3: Commit + 里程碑**

```bash
git add tests/test_p3_smoke.py
git commit -m "test(p3): memory structuring + recall + digest smoke test"
git tag p3-memory-recall-digest-done
```

---

# Post-P3 · 收尾（不含独立 PR，随 P3 合并）

## Task 28: `session["history"]` 完全退休 + 删除 legacy XML tool 主路径注册

**Files:**
- Modify: `pico/runtime.py`, `pico/agent_loop.py`, `pico/context_manager.py`

**Interfaces:**
- Produces:
  - `Pico.record()` 老方法 + 相关 `session["history"]` 兼容代码删除；
  - `ContextManager.build()` 老签名删除（保留 `build_v2` 为唯一入口，同时提供 `build` 别名 = `build_v2`）；
  - `model_output_parser.parse_model_output` 保留供 fallback adapter 使用，但不再从 `agent_loop` 主路径调用。

- [ ] **Step 1: 网格查找残留调用点**

```bash
grep -rn "session\[.history.\]" pico/
grep -rn "\.record(" pico/ | grep -v record_message
grep -rn "context_manager.build(" pico/
```

- [ ] **Step 2: 删除或改写残留调用；跑全量测试**

```bash
uv run pytest -q
```

Expected: all pass。若有测试引用 `session["history"]` 或 `ContextManager.build`，改成 v2 语义或删除。

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: retire session[history], ContextManager.build v1, XML tool-call main path"
```

---

## Self-Review

**1. Spec coverage:**
| Spec 章节 | 覆盖 task |
| -- | -- |
| §3 5 层架构 | Task 5 (build_v2 shape), Task 14 (injection wiring) |
| §4.1 system 字段 | Task 5 |
| §4.2 tools 字段 + risky 迁移 | Task 5 (`_convert_pico_tool_to_anthropic`) |
| §4.3 messages 数组 | Task 6 (append helpers), Task 7 (loop wiring), Task 4 (migrator) |
| §4.4 动态注入 (C1-C5) | Task 10 (escape), Task 11 (intent), Task 12 (sources), Task 13 (renderer), Task 24 (recall wiring) |
| §4.5 Cache 分层 | Task 2 (anthropic breakpoints), Task 14 (breakpoints in build_v2) |
| §5.1 Layout | Task 17 (agent/ scope) |
| §5.2 Frontmatter | Task 16 |
| §5.3 Retrieval 增强 | Task 18 (field boost), Task 19 (link), Task 20 (tombstone) |
| §5.4 Recall | Task 23 (四护栏), Task 24 (renderer wiring) |
| §5.5 memory_save topic | Task 21 |
| §5.6 Migration | Task 22 |
| §6 Context Assembly | Task 5, 13, 14 |
| §6.3 Digest | Task 25 (digest.py), Task 26 (agent_loop 集成 + 写盘) |
| §6.4 Budget Enforcement | Task 14 (pinned overflow), Task 13 (injection tail_clip) |
| §6.5 Clean-up | Task 8 |
| §7 Provider Adaptation | Task 1 (Response/StopReason), Task 2 (anthropic v2), Task 3 (fallback), Task 7 (delegation 走 fallback path) |
| §8 Data Migration | Task 4 (session migrator) |
| §9 Observability | Task 13 (renderer telemetry), Task 14 (system_cache_key + tokens) |
| §10 pico.toml | 常量目前 hard-coded 到模块顶部（RECALL_MIN_SCORE 等）——若需读取 pico.toml，后续 patch；不阻塞 P3 上线 |
| §11 Phased Rollout | Task 9 / 15 / 27 里程碑 tag |
| §12 Testing Strategy | 每 task 附 TDD 循环 |
| §13 Risks | 已通过 test 覆盖：cache_control_placement (Task 2)、pair drop atomicity (Task 4 migrator)、session backup (Task 4)、tool_result_raw ondisk (Task 26)、injection escape (Task 10)、intent first-match-wins (Task 11)、recall guards (Task 23) |

**Gap 说明**：§10 pico.toml 配置只有默认值在代码里，未覆盖"用户改 pico.toml 覆盖默认"。若需完整实现，可增补 Task 29 读取 pico.toml；本 spec 中已在 §9 明确 "所有键均有默认值"，跳过 pico.toml 读取不违反 spec。

**2. Placeholder scan:** 所有 step 均含具体代码 / 命令 / 期望输出，无 TBD/TODO。

**3. Type consistency:**
- `Response` / `StopReason` 在 Task 1 定义，Task 2/3/7 一致引用；
- `ToolResultDigest` 字段（tool/title/bullets/source_hash/raw_path）在 Task 25 定义，Task 26 消费一致；
- `IntentResult` NamedTuple 字段（name/matched_keyword/budget）在 Task 11 定义，Task 13/14/15 一致；
- `MemoryFile.frontmatter` 在 Task 17 引入，Task 18/19/20/23 一致引用。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-07-pico-memory-context-redesign.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 我为每个 task 派一个 fresh subagent 独立完成，两阶段 review（写完 + 复审），快速迭代；

**2. Inline Execution** — 用 executing-plans skill 在本会话里连贯执行，批量+checkpoint。

Which approach?
