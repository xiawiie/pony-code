# Pico Interview Breakdown Design

- Date: 2026-07-06
- Status: Approved structure, ready for interview-material drafting
- Scope: Produce a detailed interview-facing project breakdown for Pico.
- Audience: The candidate using Pico as a project experience, plus a senior interviewer evaluating the project.
- Language: Chinese main text, with Pico's established English domain terms preserved where they map directly to code or docs.

## 1. Purpose

This document defines how to turn the Pico repository into a complete interview-preparation artifact.

The target output is not another code audit. It should help the candidate explain Pico as a serious local coding-agent harness, then withstand senior-interviewer follow-up questions about architecture, safety, recovery, memory, provider abstraction, evaluation, and tradeoffs.

The output should have two layers:

1. Candidate-facing project narrative: what Pico is, what problem it solves, what the candidate built, how the architecture works, and what results can be claimed.
2. Interviewer-facing deep-dive pack: likely questions, why those questions matter, what a strong answer sounds like, and which boundaries should be stated honestly.

## 2. Current Project Facts To Preserve

The material must stay anchored to the current repository instead of inventing achievements.

Current verified repo facts from this session:

- Pico is a Python local coding-agent harness, not a thin CLI wrapper.
- Recommended command is `pico-cli`; `pico` remains as a compatibility script but can collide with macOS `/usr/bin/pico`.
- The repo contains about 66 Python files under `pico/`, about 12.4k Python source lines, and 55 test files.
- The core runtime path is `pico.cli` -> `pico.runtime.Pico` -> `pico.agent_loop.AgentLoop` -> `pico.tool_executor.ToolExecutor`.
- The main architecture areas are CLI surface, runtime control loop, prompt/context management, tool policy and execution, recoverable editing, memory v2, provider adapters, run artifacts, and benchmark/evaluation evidence.
- Run artifacts are stored under `.pico/runs/<run_id>/` as `task_state.json`, `trace.jsonl`, and `report.json`.
- Recoverable editing uses a separate `.pico/checkpoints/` store with checkpoint records, tool-change records, and content-addressed blobs.
- Memory v2 is split across `AGENTS.md`, `.pico/memory/notes/*.md`, and `.pico/memory/agent_notes.md`.
- Retrieval is lexical BM25 plus CJK bigram matching, not semantic embedding retrieval.
- Provider support includes Ollama, OpenAI-compatible Responses, Anthropic-compatible Messages, and DeepSeek through the Anthropic-compatible path.
- The canonical local gate is `./scripts/check.sh`, which runs ruff and pytest.
- The current branch has unrelated local changes and untracked superpowers drafts that this interview material should not modify or claim as submitted.

## 3. Output Shape

The final interview material should be organized as a practical preparation document, not as a generic README rewrite.

### 3.1 First-Layer Narrative

Include:

- One-sentence project positioning.
- Resume-ready project bullets.
- A 30-second version.
- A 2-minute version.
- An 8-10-minute expanded project explanation.
- A "what I was responsible for" section that can be adapted if the candidate wants to claim full ownership or partial ownership.
- A "what not to overclaim" section.

### 3.2 System Module Breakdown

Cover these modules in a consistent format:

- CLI Surface
- Runtime and AgentLoop
- ContextManager
- ToolExecutor and tool registry
- Safe Execution and command policy
- Recoverable Editing, checkpoint store, and restore flow
- Memory v2 and RepoMap
- Provider adapters
- Run artifacts, trace, and report
- Benchmark and release evidence

For each module, include:

- What it does.
- Why it exists.
- Main code anchors.
- Main design tradeoff.
- Interview talking points.
- Likely follow-up question.
- Strong answer outline.

### 3.3 Technical Difficulty Section

The main difficulties should be framed around engineering constraints rather than buzzwords:

- How to make a model-controlled CLI auditable.
- How to constrain tool execution without pretending to have a full OS sandbox.
- How to capture file changes for recovery without snapshotting the whole workspace.
- How to assemble bounded prompt context while preserving the current user request.
- How to design durable memory without making it an unbounded mutable scratchpad.
- How to support multiple model protocols while keeping runtime assumptions simple.
- How to prove behavior through tests, traces, reports, and deterministic benchmarks.

### 3.4 Interviewer Follow-Up Pack

The follow-up pack should include questions grouped by topic:

- Project motivation and product boundary.
- Architecture decomposition.
- Control loop and prompt construction.
- Tool policy and security.
- Recovery and checkpoint semantics.
- Memory system design.
- Provider abstraction.
- Testing and quality gates.
- Performance and scalability.
- Tradeoffs, weaknesses, and next steps.

Each question should include:

- What the interviewer is really testing.
- A strong answer.
- A conservative answer if the candidate wants to avoid overclaiming.
- A common weak answer to avoid.

## 4. Interview Positioning

Pico should be positioned as:

> A lightweight local coding-agent harness for repository-grounded engineering work. It wraps a model with workspace context, explicit tools, execution policy, local memory, recoverable editing, run artifacts, and evaluation evidence.

Avoid positioning Pico as:

- A full IDE.
- A general autonomous agent platform.
- A replacement for Git.
- A production-grade OS sandbox.
- A semantic long-term memory system.
- A mature commercial product.

The strongest interview framing is:

> I focused on making model-driven code editing inspectable and recoverable. The hardest part was not calling an LLM; it was building the harness around the model: bounded context, explicit tools, policy checks, checkpoints, trace artifacts, memory boundaries, and tests that prove these behaviors.

## 5. Evidence Strategy

The final material should mention evidence only where it can be backed by this repo.

Strong evidence:

- `README.md` for product and CLI positioning.
- `CONTEXT.md` for domain vocabulary.
- `docs/architecture/agent-harness-v1-overview.md` for runtime flow and run artifacts.
- `docs/memory-model.md` for memory surfaces.
- `docs/review-pack/README.md` for review and benchmark evidence.
- `pico/runtime.py`, `pico/agent_loop.py`, `pico/context_manager.py`, `pico/tool_executor.py`, `pico/tools.py`, `pico/recovery_manager.py`, `pico/memory/*`, and `pico/providers/*` for code anchors.
- `tests/` for safety, memory, recovery, provider, CLI, and benchmark tests.
- `benchmarks/memory_quality/run_benchmark.py` and `pico/evaluation/provider_benchmark.py` for release evidence.

Evidence wording should distinguish:

- "Implemented" when code exists.
- "Covered by tests" when tests were inspected or named.
- "Locally verifiable" when a command exists.
- "Current snapshot" when counts or working-tree state may drift.

## 6. Risks and Boundaries

The material should actively prepare the candidate to answer weaknesses:

- `runtime.py` and `tool_executor.py` remain large coordination files.
- Safe execution is policy-driven, not an OS-level sandbox.
- Restore is file-level and conservative; it does not attempt hunk-level merge or Git replacement.
- Memory retrieval is lexical, so it is auditable but not semantically powerful.
- Provider behavior differs by protocol; unified runtime shape does not erase API-specific differences.
- Evaluation evidence is stronger for deterministic harness behavior than for live-model quality.

These are not failures to hide. They are useful design boundaries to state before the interviewer turns them into objections.

## 7. Proposed Final Artifact

The final artifact should be written as a long Chinese Markdown document named:

`docs/superpowers/specs/2026-07-06-pico-interview-breakdown.md`

It should include:

1. Project positioning.
2. Resume bullets.
3. 30-second, 2-minute, and 8-10-minute speaking scripts.
4. System architecture overview.
5. Module-by-module breakdown.
6. Core technical challenges.
7. Results and evidence.
8. Weaknesses and future work.
9. Interviewer question bank.
10. Answer templates and anti-patterns.
11. Final cheat sheet.

The response to the user should also summarize the artifact and provide the most important parts inline, because the user asked for material they can directly study.

## 8. Acceptance Criteria

The final material is acceptable when:

- It is detailed enough for a senior technical interview.
- It separates candidate narrative from interviewer deep-dive questions.
- It does not invent business metrics, user counts, or production deployment claims.
- It cites concrete Pico modules and docs where useful.
- It clearly names design tradeoffs and limitations.
- It includes practical answer templates, not only abstract analysis.
- It can be used both for resume writing and oral interview preparation.

## 9. Non-Goals

- Do not modify Pico source code.
- Do not change tests or benchmarks.
- Do not resolve existing unrelated worktree changes.
- Do not run live provider benchmarks.
- Do not create a visual/browser companion.
- Do not turn this into a broad implementation roadmap.

