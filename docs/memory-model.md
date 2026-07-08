# Pico Memory Model (v2)

Pico stores project knowledge across three surfaces. Together they cover
"read at every session" convention text, free-form user knowledge, and
lessons the agent captured on request.

## 1. Project conventions (AGENTS.md)

`<repo>/AGENTS.md` and (optionally) `~/.pico/AGENTS.md` are read at the
start of every session. Write project-wide rules, tool preferences, and
conventions here.

Pico reads AGENTS.md — not CLAUDE.md. If you already keep a CLAUDE.md,
run `ln -s CLAUDE.md AGENTS.md` inside the repo so Pico picks it up. The
`pico-cli doctor` command flags this situation with an info hint.

## 2. User notes (agent-readable only)

Any Markdown file under `<repo>/.pico/memory/notes/**/*.md` or
`~/.pico/memory/notes/**/*.md`. Free-form. The agent reads via
`memory_read` and `memory_search` tools; it cannot modify these files
under any tool — protecting your hand-authored context.

Example: `.pico/memory/notes/auth.md`

```
# Auth design notes

- bcrypt rounds must be <= 12 (CI timeout above)
- session cookie: SameSite=Lax
```

## 3. Agent lessons (agent_notes.md)

When you explicitly ask the agent to remember something, it appends a
short timestamped line to `.pico/memory/agent_notes.md` (workspace) or
`~/.pico/memory/agent_notes.md` (global). Append-only, atomic writes,
one entry per line.

Soft size limit: 8000 characters. Beyond that, Pico prints a one-shot
stderr warning suggesting `pico-cli memory review`.

## CLI

```
pico-cli memory list                 # list all memory files
pico-cli memory show <path>          # print one memory file
pico-cli memory search <query>       # BM25 + CJK bigram search
pico-cli memory review               # show agent_notes.md + edit hint
pico-cli memory migrate [--apply]    # migrate legacy topics/ into notes/
```

## REPL

```
/save <text>       append note to workspace agent_notes.md
/memory-review     same as pico-cli memory review
/memory            compact working memory: task:, recent:, blank line, then Memory files: with .pico/memory/ character counts
```

## Limitations

- Retrieval is keyword-based (BM25 + CJK bigram). Semantic equivalents
  such as "身份认证" vs "auth" are not linked automatically.
- Symbol lookup (`repo_lookup`) is precise for Python (AST), best-effort
  for TS/JS/Go/Rust (regex).
- Memory and sessions are independent axes; there is no cross-session
  memory rewind.

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
