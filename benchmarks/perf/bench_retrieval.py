"""Benchmark Retrieval.search across note counts."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf.harness import bench  # noqa: E402
from pony.memory.block_store import BlockStore  # noqa: E402
from pony.memory.retrieval import Retrieval  # noqa: E402


def _populate(root, count):
    (root / "notes").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / "notes" / f"note-{i}.md").write_text(
            f"---\nname: note-{i}\ntype: feedback\ndescription: cache and memory topic {i}\n---\n"
            f"Body text mentioning cache invalidation and memory retrieval. Note {i}.\n"
            f"See [[note-{(i + 1) % count}]] for related.\n",
            encoding="utf-8",
        )


def main():
    scenarios = []
    for name, count in [("small", 10), ("medium", 100), ("large", 1000)]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _populate(root, count)
            store = BlockStore(workspace_root=root, user_root=root / "user")
            ret = Retrieval(store)
            result = bench(f"retrieval/{name}", lambda: ret.search("cache memory"), iterations=50)
            scenarios.append(result)
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
