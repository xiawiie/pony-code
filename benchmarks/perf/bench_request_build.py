"""Benchmark ContextManager.build_request across session sizes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pony.context.renderer import render_current_user_message  # noqa: E402
from pony.agent.context_manager import ContextManager  # noqa: E402


def _make_agent(session_len):
    a = MagicMock()
    a.prefix = "SYSTEM " * 100  # ~700 chars
    a.tools = {
        "read_file": {"schema": {"path": "str"}, "risky": False, "description": "read"},
        "run_shell": {"schema": {"command": "str"}, "risky": True, "description": "run"},
    }
    a.session = {
        "messages": [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"msg-{i} " * 20,
                "_pony_meta": {"created_at": "2026-07-10T00:00:00+00:00"},
            }
            for i in range(max(2, session_len + session_len % 2))
        ]
    }
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="branch: main\nstatus: clean\n")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}
    return a


def main():
    scenarios = []
    for name, size in [("small", 1), ("medium", 30), ("large", 300)]:
        agent = _make_agent(size)
        cm = ContextManager(agent)
        user_message = "test question"
        agent.session["messages"].append(
            {
                "role": "user",
                "content": user_message,
                "_pony_meta": {"created_at": "2026-07-10T00:00:00+00:00"},
            }
        )
        snapshot, telemetry = render_current_user_message(agent, user_message)
        result = bench(
            f"build_request/{name}",
            lambda: cm.build_request(
                injection_snapshot=snapshot,
                injection_telemetry=telemetry,
                preflight_metadata={},
            ),
            iterations=100,
        )
        scenarios.append(result)
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
