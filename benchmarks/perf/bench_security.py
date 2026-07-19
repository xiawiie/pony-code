"""Benchmark retained Host security primitives."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from benchmarks.perf.harness import bench
from pony.security.command_policy import assess_command
from pony.security.redaction import redact_artifact


SCENARIO_NAMES = (
    "security/redact_artifact/100",
    "shell/assess_corpus/50",
)


def main():
    secrets = {f"PONY_TOKEN_{index}": f"ghp_{index:032d}" for index in range(100)}
    artifact = {
        "items": list(secrets.values()),
        "nested": [{"token": value} for value in secrets.values()],
    }
    commands = ["pwd", "python -m pytest -q", "cat .env", "pwd | wc -l"]
    command_batch = [commands[index % len(commands)] for index in range(50)]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "README.md").write_text("safe\n", encoding="utf-8")
        scenarios = [
            bench(
                SCENARIO_NAMES[0],
                lambda: redact_artifact(
                    artifact,
                    env=secrets,
                    secret_env_names=tuple(secrets),
                ),
                iterations=20,
            ),
            bench(
                SCENARIO_NAMES[1],
                lambda: [assess_command(command, root) for command in command_batch],
                iterations=20,
            ),
        ]
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
