"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少稳定 prefix、历史状态以及当前用户请求送进模型。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


DEFAULT_TOTAL_BUDGET = 15000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 7000,
    "history": 8000,
}


MEMORY_USAGE_GUIDANCE = """<memory_usage_guidance>
- Use memory_save ONLY when the user explicitly asks to remember/save something.
- Do NOT save routine tool results, file paths, or turn-scoped state.
- Good candidates: cross-session lessons, design decisions, environment gotchas.
- Bad candidates: "I read auth.py", "current turn diff", "pytest passed".
</memory_usage_guidance>"""

MEMORY_READING_GUIDANCE = """<memory_reading_guidance>
Before answering about the codebase or a past decision:
- If <memory_index> shows a relevant file, consider memory_read.
- For symbol location, prefer repo_lookup over manual search.
- For keyword lookup across notes, use memory_search.
</memory_reading_guidance>"""
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("history", "prefix")
SECTION_ORDER = ("prefix", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _hash_text(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _convert_pico_tool_to_anthropic(name, spec):
    """Convert one pico BASE_TOOL_SPECS entry to an Anthropic tool schema entry.

    Rules (see task-5 brief):
    - Each arg in `schema` is treated as string by default; if the sig contains
      "int" it becomes {"type": "integer"}. Everything else falls through to string
      (this includes bool / list / list[str] etc — deliberate P1 simplification).
    - An arg is required iff its sig has no `=` default marker.
    - `risky: True` -> append ` Requires user approval before execution.` to description.
    """
    props = {}
    required = []
    for arg_name, sig in (spec.get("schema") or {}).items():
        sig_str = str(sig)
        if "int" in sig_str:
            props[arg_name] = {"type": "integer"}
        else:
            props[arg_name] = {"type": "string"}
        if "=" not in sig_str:
            required.append(arg_name)
    desc = str(spec.get("description", "") or "")
    if spec.get("risky"):
        desc = (desc + " Requires user approval before execution.").strip()
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


def _build_tools_list(pico_tools):
    """Convert the whole pico tool dict to an Anthropic-shaped tools list.

    Sorted by name for deterministic output: identical inputs always produce
    identical tool arrays, which keeps prompt-cache keys stable across turns.
    """
    if not pico_tools:
        return []
    return [_convert_pico_tool_to_anthropic(name, spec) for name, spec in sorted(pico_tools.items())]


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        # Lazy import to avoid cycle
        from pico.memory.refresher import MemoryRefresher
        self._refresher: MemoryRefresher | None = None

    def _get_refresher(self):
        if self._refresher is None:
            store = getattr(self.agent, "memory_store", None)
            repo_map = getattr(self.agent, "repo_map", None)
            if store is None or repo_map is None:
                return None
            from pico.memory.refresher import MemoryRefresher
            self._refresher = MemoryRefresher(store, repo_map)
        return self._refresher

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 历史状态 +
        当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext`、v2 memory index/project
        structure 和会话 history 提供上下文，这个函数则把它们和当前请求合成一份
        可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        # v2: memory index + project structure (both go into stable prefix)
        refresher = self._get_refresher()
        memory_index_text = ""
        project_structure_text = ""
        if refresher is not None:
            snap = refresher.refresh_if_stale()
            memory_index_text = snap.memory_index_text
            project_structure_text = snap.project_structure_text
        base_prefix = str(getattr(self.agent, "prefix", ""))
        composed_prefix_parts = [base_prefix]
        composed_prefix_parts.append(MEMORY_USAGE_GUIDANCE)
        composed_prefix_parts.append(MEMORY_READING_GUIDANCE)
        if project_structure_text:
            composed_prefix_parts.append(project_structure_text)
        if memory_index_text:
            composed_prefix_parts.append(memory_index_text)
        # volatile head: workspace_state (branch/status/commits) lives with history,
        # keeping the stable prefix cache key independent from branch/status churn.
        workspace_state_text = ""
        if hasattr(self.agent, "workspace") and hasattr(self.agent.workspace, "volatile_text"):
            workspace_state_text = str(self.agent.workspace.volatile_text() or "").strip()
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        section_texts = {
            "prefix": "\n\n".join(p for p in composed_prefix_parts if p),
            "history": {
                "workspace_state": workspace_state_text,
                "checkpoint_text": checkpoint_text,
            },
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                user_message=user_message,
                section_texts=section_texts,
                base_prefix=base_prefix,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(section_texts, budgets)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 history，然后才动 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            user_message=user_message,
            section_texts=section_texts,
            base_prefix=base_prefix,
        )
        return prompt, metadata

    def build_v2(self, user_message):
        """Assemble one turn as the Anthropic message-array shape.

        Task 5 sibling to `build()`: instead of a single flat prompt string, this
        returns a `request` dict with `system` / `tools` / `messages` /
        `cache_control_breakpoints` — the shape `providers.anthropic.complete_v2`
        expects. `build()` is deliberately unchanged; Task 7 will migrate the
        agent loop to call `build_v2`.

        Layout:
        - `system`  : single text block carrying the stable `agent.prefix`, with
                      `cache_control: ephemeral` so the provider can reuse the
                      prompt-cache entry across turns.
        - `tools`   : `agent.tools` (pico's internal dict) converted to Anthropic
                      shape, sorted by name for cache-stable ordering.
        - `messages`: a shallow copy of `agent.session["messages"]` with the current
                      user turn appended. The copy matters — we must not mutate
                      the session's history when we append.
        - `cache_control_breakpoints`: `[len(messages) - 2]` when there are 2+
                      messages, else `[]`. Task 8 will drive an actual
                      cache-control block placement from this index.
        """
        user_message = str(user_message)
        system_text = str(getattr(self.agent, "prefix", "") or "")
        system_block = {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
        system = [system_block]

        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})

        # Shallow copy so the append below cannot mutate agent.session["messages"].
        session = getattr(self.agent, "session", {}) or {}
        messages = list(session.get("messages", []) or [])

        # Task 5: plain-text user content. Task 14 will wrap this in
        # <system-reminder> injection.
        messages.append({"role": "user", "content": user_message})

        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []

        system_cache_key = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        metadata = {
            "system_cache_key": system_cache_key,
            "messages_count": len(messages),
            "cache_control_breakpoints": list(breakpoints),
            # Task 8 will drop this alias; kept for now so callers of `build()`
            # that reach for `metadata["prompt_cache_key"]` don't break if they
            # happen to migrate to `build_v2` first.
            "prompt_cache_key": system_cache_key,
        }
        request = {
            "system": system,
            "tools": tools,
            "messages": messages,
            "cache_control_breakpoints": breakpoints,
        }
        return request, metadata

    def _render_sections_without_reduction(self, section_texts):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._history_section_raw(history, section_texts["history"])
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: int(DEFAULT_SECTION_FLOORS.get(section, max(20, int(budget) // 4)))
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0), section_texts["history"])
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_history_section(self, budget, history_head=""):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._history_section_raw(history, history_head)
        history_head_rendered = self._render_history_head(history_head, max(0, budget - len("Transcript:") - 2))
        transcript_budget = budget
        if history_head_rendered:
            transcript_budget = max(len("Transcript:"), budget - len(history_head_rendered) - 2)
        if not history:
            rendered = self._empty_transcript_text(transcript_budget)
            if history_head_rendered:
                rendered = history_head_rendered + "\n\n" + rendered
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= transcript_budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = transcript_budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= transcript_budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= transcript_budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])
        if history_head_rendered:
            rendered = history_head_rendered + "\n\n" + rendered

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        session = getattr(self.agent, "session", {})
        if not isinstance(session, dict):
            return ""
        memory = session.get("memory", {})
        if not isinstance(memory, dict):
            return ""
        entry = memory.get("file_summaries", {}).get(str(path))
        if not isinstance(entry, dict):
            return ""
        return entry.get("summary", "")

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _history_head_parts(self, history_head):
        if isinstance(history_head, dict):
            return (
                str(history_head.get("workspace_state", "") or "").strip(),
                str(history_head.get("checkpoint_text", "") or "").strip(),
            )
        return (str(history_head or "").strip(), "")

    def _render_history_head(self, history_head, budget):
        workspace_state, checkpoint_text = self._history_head_parts(history_head)
        budget = int(budget)
        if budget <= 0:
            return ""
        if workspace_state and checkpoint_text:
            separator_cost = 2
            text_budget = max(0, budget - separator_cost)
            checkpoint_floor = min(len(checkpoint_text), len(checkpoint_text.splitlines()[0]))
            checkpoint_budget = min(len(checkpoint_text), max(checkpoint_floor, text_budget // 3))
            workspace_budget = max(0, text_budget - checkpoint_budget)
            return "\n\n".join(
                part
                for part in (
                    self._clip_workspace_state(workspace_state, workspace_budget),
                    _tail_clip(checkpoint_text, checkpoint_budget),
                )
                if part
            )
        if workspace_state:
            return self._clip_workspace_state(workspace_state, budget)
        if checkpoint_text:
            return _tail_clip(checkpoint_text, budget)
        return ""

    def _clip_workspace_state(self, text, limit):
        text = str(text)
        limit = int(limit)
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("<workspace_state") and lines[-1].startswith("</workspace_state"):
            wrapper_cost = len(lines[0]) + len(lines[-1]) + len("\n\n")
            if limit > wrapper_cost:
                body_budget = limit - wrapper_cost
                return "\n".join([lines[0], _tail_clip("\n".join(lines[1:-1]), body_budget), lines[-1]])
        return _tail_clip(text, limit)

    def _empty_transcript_text(self, budget):
        text = "Transcript:\n- empty"
        if budget <= len("Transcript:"):
            return "Transcript:"
        return _tail_clip(text, budget)

    def _history_section_raw(self, history, history_head=""):
        raw = self._raw_history_text(history)
        workspace_state, checkpoint_text = self._history_head_parts(history_head)
        rendered_head = "\n\n".join(part for part in (workspace_state, checkpoint_text) if part)
        if not rendered_head:
            return raw
        return rendered_head + "\n\n" + raw

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered[section].rendered
                for section in SECTION_ORDER
                if rendered[section].rendered
            ]
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, user_message, section_texts, base_prefix):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        base_prefix_hash = getattr(getattr(self.agent, "prefix_state", None), "hash", _hash_text(base_prefix))
        stable_prefix_hash = _hash_text(rendered["prefix"].rendered)
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "base_prefix_hash": base_prefix_hash,
            "stable_prefix_hash": stable_prefix_hash,
            "prefix_hash": stable_prefix_hash,
            "prompt_cache_key": stable_prefix_hash,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
