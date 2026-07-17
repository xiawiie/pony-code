from __future__ import annotations

import json
import tempfile
from pathlib import Path

from benchmarks.perf.harness import bench
from pico.state.checkpoint_store import CheckpointStore
from pico.recovery.manager import RecoveryManager
from pico.recovery.models import new_checkpoint_record
from pico.recovery.policy import assess_command
from pico.security.redaction import redact_artifact
from pico.tools.change_recorder import ToolChangeRecorder


SCENARIO_NAMES = (
    "security/redact_artifact/100",
    "shell/assess_corpus/50",
    "recovery/pending_reviews/200",
    "recovery/preview/100",
)


def _restore_fixture(root, count):
    store = CheckpointStore(root)
    entries = []
    for index in range(count):
        name = f"file-{index}.txt"
        before_bytes = f"before-{index}".encode()
        after_bytes = f"after-{index}".encode()
        before = store.write_blob(before_bytes)
        after = store.write_blob(after_bytes)
        (root / name).write_bytes(after_bytes)
        entries.append(
            {
                "path": name,
                "change_kind": "modified",
                "snapshot_eligible": True,
                "ineligible_reason": "",
                "before_exists": True,
                "before_blob_ref": before["blob_ref"],
                "before_hash": before["content_hash"],
                "before_mode": 0o600,
                "after_exists": True,
                "after_blob_ref": after["blob_ref"],
                "after_hash": after["content_hash"],
                "after_mode": 0o600,
                "expected_current_hash": after["content_hash"],
                "source_tool_change_ids": [],
            }
        )
    record = new_checkpoint_record(
        "ckpt_perf",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(root.resolve()),
    )
    record["file_entries"] = entries
    store.write_checkpoint_record(record)
    return store, RecoveryManager(store, root)


def main():
    secrets = {f"PICO_TOKEN_{index}": f"ghp_{index:032d}" for index in range(100)}
    artifact = {
        "items": list(secrets.values()),
        "nested": [{"token": value} for value in secrets.values()],
    }
    commands = ["pwd", "python -m pytest -q", "cat .env", "pwd | wc -l"]
    command_batch = [commands[index % len(commands)] for index in range(50)]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "README.md").write_text("safe\n", encoding="utf-8")
        store, manager = _restore_fixture(root, 100)
        recorder = ToolChangeRecorder(store, owner_id="perf-owner")
        for index in range(200):
            recorder.start(
                "",
                f"turn-{index}",
                "write_file",
                "workspace_write",
                {"path": f"pending-{index}.txt"},
            )

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
            bench(
                SCENARIO_NAMES[2],
                recorder.pending_recovery_reviews,
                iterations=20,
            ),
            bench(
                SCENARIO_NAMES[3],
                lambda: manager.preview_restore("ckpt_perf"),
                iterations=20,
            ),
        ]
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
