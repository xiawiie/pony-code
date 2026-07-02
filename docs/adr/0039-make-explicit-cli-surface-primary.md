# Make explicit CLI surface primary

Pico will make explicit subcommands the primary CLI Surface while keeping the existing bare prompt and no-argument REPL behavior as compatibility paths. This preserves the first-phase focus on Recoverable Editing and Safe Execution, avoids a catch-all command shape that blocks future subcommands, and gives both humans and agentic workflows stable commands for run, REPL, status, diagnostics, runs, sessions, checkpoints, and user-initiated recovery actions.
