from pony.memory.service import (
    invalidate_file_summary_dict,
    invalidate_stale_file_summaries_dict,
    normalize_file_summaries_dict,
    set_file_summary_dict,
)


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


def test_normalize_file_summaries_dict_accepts_mapping_and_plain_text(tmp_path):
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
