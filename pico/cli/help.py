"""Discoverable slash commands shared by the REPL and TUI."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    summary: str


SLASH_COMMANDS = (
    SlashCommand("/help", "/help", "Show interactive commands"),
    SlashCommand("/memory", "/memory", "Show working memory and memory files"),
    SlashCommand("/memory-review", "/memory-review", "Review agent notes"),
    SlashCommand("/remember", "/remember <text>", "Append an explicit workspace note"),
    SlashCommand("/session", "/session", "Show the active session file"),
    SlashCommand("/tree", "/tree", "Show the append-only Session Tree"),
    SlashCommand("/compact", "/compact [focus]", "Compact older conversation history"),
    SlashCommand("/checkpoint", "/checkpoint [label]", "Create a task checkpoint"),
    SlashCommand("/fork", "/fork <entry>", "Branch the conversation at an entry"),
    SlashCommand(
        "/rewind",
        "/rewind <entry> [--workspace] [--summary[=focus]]",
        "Rewind the session, optionally restoring workspace files",
    ),
    SlashCommand(
        "/clone",
        "/clone --to-worktree <path>",
        "Clone the active branch to another worktree",
    ),
    SlashCommand("/reset", "/reset", "Clear messages and working memory"),
    SlashCommand("/exit", "/exit", "Exit Pico"),
    SlashCommand("/quit", "/quit", "Exit Pico (alias of /exit)"),
)


HELP_DETAILS = "Commands:\n" + "\n".join(
    f"{command.usage:<54} {command.summary}." for command in SLASH_COMMANDS
)
