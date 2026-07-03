from pico.features.memory import (
    LayeredMemory,
    invalidate_file_summary_dict,
    invalidate_stale_file_summaries_dict,
    normalize_file_summaries_dict,
    set_file_summary_dict,
)


def test_working_memory_tracks_summary_and_recent_files():
    memory = LayeredMemory()

    memory.set_task_summary("Investigate flaky tests")
    memory.remember_file("README.md")
    memory.remember_file("src/app.py")
    memory.remember_file("README.md")

    snapshot = memory.to_dict()

    assert snapshot["working"]["task_summary"] == "Investigate flaky tests"
    assert snapshot["working"]["recent_files"] == ["src/app.py", "README.md"]
    assert snapshot["task"] == "Investigate flaky tests"
    assert snapshot["files"] == ["src/app.py", "README.md"]


def test_episodic_notes_append_and_retrieve_deterministically():
    memory = LayeredMemory()

    memory.append_note("Exact tag note", tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    memory.append_note("Keyword overlap note about memory", created_at="2026-04-07T10:01:00+00:00")
    memory.append_note("Newest unrelated note", created_at="2026-04-07T10:02:00+00:00")
    memory.append_note("Older unrelated note", created_at="2026-04-07T09:59:00+00:00")

    snapshot = memory.to_dict()
    assert [note["text"] for note in snapshot["episodic_notes"]] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]
    assert snapshot["notes"] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]

    lines = [line for line in memory.retrieval_view("recall memory", limit=4).splitlines() if line.startswith("- ")]
    assert lines == [
        "- Exact tag note",
        "- Keyword overlap note about memory",
    ]


def test_file_summaries_use_canonical_paths_and_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    memory.remember_file("./sample.txt")
    snapshot = memory.to_dict()["file_summaries"]["sample.txt"]

    assert snapshot["summary"] == "sample.txt: alpha"
    assert snapshot["freshness"]

    assert "sample.txt: alpha" in memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")
    assert "sample.txt: alpha" not in memory.render_memory_text()

    memory.invalidate_file_summary("sample.txt")

    assert "sample.txt" not in memory.to_dict()["file_summaries"]


def test_set_file_summary_dict_mutates_and_invalidates_in_place(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    summaries = {}

    result = set_file_summary_dict(
        summaries,
        "./sample.txt",
        "sample.txt: alpha",
        workspace_root=tmp_path,
    )

    assert result is summaries
    assert list(summaries) == ["sample.txt"]
    assert summaries["sample.txt"]["summary"] == "sample.txt: alpha"
    assert summaries["sample.txt"]["freshness"]

    result = invalidate_file_summary_dict(summaries, "./sample.txt", workspace_root=tmp_path)

    assert result is summaries
    assert summaries == {}


def test_normalize_file_summaries_dict_accepts_legacy_shapes(tmp_path):
    file_path = tmp_path / "sample.txt"
    notes_path = tmp_path / "notes.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    notes_path.write_text("notes\n", encoding="utf-8")

    normalized = normalize_file_summaries_dict(
        {
            str(file_path): {
                "summary": "absolute summary",
                "created_at": "2026-07-03T10:00:00+08:00",
                "freshness": "fresh",
            },
            "./notes.txt": "plain summary",
            "": "empty key summary",
        },
        workspace_root=tmp_path,
    )

    assert normalized == {
        "sample.txt": {
            "summary": "absolute summary",
            "created_at": "2026-07-03T10:00:00+08:00",
            "freshness": "fresh",
        },
        "notes.txt": {
            "summary": "plain summary",
            "created_at": normalized["notes.txt"]["created_at"],
            "freshness": normalized["notes.txt"]["freshness"],
        },
    }
    assert normalized["notes.txt"]["created_at"]
    assert normalized["notes.txt"]["freshness"]


def test_invalidate_stale_file_summaries_dict_removes_changed_file(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    summaries = normalize_file_summaries_dict(
        {"./sample.txt": "sample.txt: alpha"},
        workspace_root=tmp_path,
    )

    file_path.write_text("beta\n", encoding="utf-8")
    invalidated = invalidate_stale_file_summaries_dict(summaries, workspace_root=tmp_path)

    assert invalidated == ["sample.txt"]
    assert summaries == {}


def test_process_notes_keep_kind_and_latest_duplicate_wins():
    memory = LayeredMemory()

    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:00:00+00:00",
        kind="process",
    )
    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:01:00+00:00",
        kind="process",
    )

    notes = memory.to_dict()["episodic_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "process"
    assert notes[0]["created_at"] == "2026-04-07T10:01:00+00:00"
