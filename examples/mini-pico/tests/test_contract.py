import subprocess
import sys
from pathlib import Path

import mini_pico


def test_mini_pico_module_and_public_exports():
    assert mini_pico.Pico is not None
    assert mini_pico.FakeModelClient is not None
    assert not hasattr(mini_pico, "MiniAgent")
    result = subprocess.run([sys.executable, "-m", "mini_pico", "--help"], capture_output=True, text=True, check=True)
    assert "Teaching-sized Pico agent harness" in result.stdout


def test_readme_main_mapping_points_to_existing_files():
    repo_root = Path(__file__).resolve().parents[3]
    main_files = [
        "pico/cli.py",
        "pico/runtime.py",
        "pico/agent_loop.py",
        "pico/context_manager.py",
        "pico/providers/clients.py",
        "pico/tool_executor.py",
        "pico/tools.py",
        "pico/task_state.py",
        "pico/run_store.py",
        "pico/workspace.py",
    ]
    for path in main_files:
        assert (repo_root / path).exists()
