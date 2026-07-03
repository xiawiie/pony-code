from pico.working_memory import RECENT_FILES_LIMIT, TASK_SUMMARY_LIMIT, WorkingMemory


def test_class_constants_are_exposed():
    assert WorkingMemory.TASK_SUMMARY_LIMIT == 300
    assert WorkingMemory.RECENT_FILES_LIMIT == 8
    assert TASK_SUMMARY_LIMIT == WorkingMemory.TASK_SUMMARY_LIMIT
    assert RECENT_FILES_LIMIT == WorkingMemory.RECENT_FILES_LIMIT


def test_defaults_empty():
    memory = WorkingMemory()

    assert memory.task_summary == ""
    assert memory.recent_files == []
    assert memory.to_dict() == {"task_summary": "", "recent_files": []}


def test_none_task_summary_is_empty_for_constructor_and_setter():
    memory = WorkingMemory(task_summary=None)

    assert memory.task_summary == ""

    memory.set_task_summary("has value")
    memory.set_task_summary(None)

    assert memory.task_summary == ""


def test_set_task_summary_truncates_to_limit():
    summary = "x" * 350
    memory = WorkingMemory()

    memory.set_task_summary(summary)

    assert len(memory.task_summary) == WorkingMemory.TASK_SUMMARY_LIMIT
    assert memory.task_summary == "x" * WorkingMemory.TASK_SUMMARY_LIMIT


def test_constructor_truncates_task_summary_to_limit():
    memory = WorkingMemory(task_summary="x" * 350)

    assert len(memory.task_summary) == WorkingMemory.TASK_SUMMARY_LIMIT
    assert memory.task_summary == "x" * WorkingMemory.TASK_SUMMARY_LIMIT


def test_remember_file_canonicalizes_dedupes_and_keeps_latest_eight(tmp_path):
    memory = WorkingMemory(workspace_root=tmp_path)

    for index in range(10):
        memory.remember_file(tmp_path / f"src/file_{index}.py")
    memory.remember_file(tmp_path / "src/file_3.py")

    assert memory.recent_files[0] == "src/file_3.py"
    assert len(memory.recent_files) == RECENT_FILES_LIMIT
    assert len(memory.recent_files) == len(set(memory.recent_files))
    assert memory.recent_files == [
        "src/file_3.py",
        "src/file_9.py",
        "src/file_8.py",
        "src/file_7.py",
        "src/file_6.py",
        "src/file_5.py",
        "src/file_4.py",
        "src/file_2.py",
    ]


def test_from_dict_reads_new_shape(tmp_path):
    memory = WorkingMemory.from_dict(
        {
            "task_summary": "  New task  ",
            "recent_files": [tmp_path / "app.py", tmp_path / "README.md", tmp_path / "app.py"],
            "unknown": "ignored",
        },
        workspace_root=tmp_path,
    )

    assert memory.to_dict() == {"task_summary": "New task", "recent_files": ["app.py", "README.md"]}


def test_from_dict_reads_v1_nested_shape(tmp_path):
    memory = WorkingMemory.from_dict(
        {
            "working": {
                "task_summary": "Nested task",
                "recent_files": [tmp_path / "README.md"],
            },
            "episodic_notes": ["ignored"],
        },
        workspace_root=tmp_path,
    )

    assert memory.to_dict() == {"task_summary": "Nested task", "recent_files": ["README.md"]}


def test_from_dict_reads_v1_flat_shape(tmp_path):
    memory = WorkingMemory.from_dict(
        {"task": "Flat task", "files": [tmp_path / "pico/runtime.py"]},
        workspace_root=tmp_path,
    )

    assert memory.to_dict() == {"task_summary": "Flat task", "recent_files": ["pico/runtime.py"]}


def test_invalid_or_none_input_gives_empty_memory():
    assert WorkingMemory.from_dict(None).to_dict() == {"task_summary": "", "recent_files": []}
    assert WorkingMemory.from_dict("not memory").to_dict() == {"task_summary": "", "recent_files": []}


def test_deprecated_v1_methods_are_absent():
    memory = WorkingMemory()

    for method_name in (
        "append_note",
        "set_file_summary",
        "invalidate_file_summary",
        "invalidate_stale_file_summaries",
        "retrieval_candidates",
        "retrieval_view",
        "render_memory_text",
        "promote_durable",
    ):
        assert not hasattr(memory, method_name)
