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
