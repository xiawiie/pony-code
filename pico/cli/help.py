import textwrap


HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help            Show this help message.
    /memory          Show working memory + a listing of memory files.
    /memory-review   Print agent_notes.md with an editing hint.
    /remember <text> Explicitly append a note to workspace agent_notes.md.
    /session         Show the path to the saved session file.
    /tree            Show the append-only Session Tree and active branch.
    /compact [focus] Compact old history into a bounded Session summary.
    /checkpoint [label]  Add a task checkpoint to the Session Tree.
    /fork <entry>    Start a new conversation branch at an entry.
    /rewind <entry> [--summary[=focus]]  Switch branch without changing files.
    /rewind <checkpoint> --workspace [--summary[=focus]]  Preview, confirm, restore, then branch.
    /clone --to-worktree <path>  Clone the active branch for another worktree.
    /reset           Clear canonical messages and working memory.
    /exit            Exit the agent.
    """
).strip()
