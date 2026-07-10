import textwrap


HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help            Show this help message.
    /memory          Show working memory + a listing of memory files.
    /memory-review   Print agent_notes.md with an editing hint.
    /save <text>     Append a note to workspace agent_notes.md.
    /session         Show the path to the saved session file.
    /reset           Clear canonical messages and working memory.
    /exit            Exit the agent.
    """
).strip()
