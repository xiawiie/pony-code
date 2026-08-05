import csv
import json
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace

import pytest

from benchmarks.memory_public import backends, run
from benchmarks.memory_public.backends import (
    Mem0Backend,
    Mem0Client,
    PonyMemoryBackend,
    clip_contexts,
    split_session_note,
)
from benchmarks.memory_public.datasets import (
    Session,
    Turn,
    load_longmemeval,
    load_persona_contexts,
    load_persona_questions,
    persona_batches,
)
from benchmarks.memory_public.scoring import (
    mcnemar_exact,
    paired_summary,
    parse_judge_answer,
    persona_correct,
    summarize_rows,
    wilson_interval,
)
from pony.agent.model_capabilities import TokenAccounting
from pony.memory.block_store import MAX_MEMORY_FILE_BYTES
from pony.providers.response import Response, StopReason


def _long_row(**overrides):
    row = {
        "question_id": "q-secret_abs",
        "question_type": "single-session-user",
        "question": "What drink do I prefer?",
        "answer": "Tea",
        "question_date": "2026-01-03",
        "haystack_session_ids": ["later", "earlier"],
        "haystack_dates": ["2026-01-02", "2026-01-01"],
        "haystack_sessions": [
            [{"role": "user", "content": "I also like water", "has_answer": False}],
            [
                {
                    "role": "user",
                    "content": "I prefer tea",
                    "has_answer": True,
                }
            ],
        ],
        "answer_session_ids": ["earlier"],
    }
    row.update(overrides)
    return row


def _write_persona_questions(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "question_id",
                "shared_context_id",
                "end_index_in_shared_context",
                "user_question_or_message",
                "all_options",
                "correct_answer",
                "question_type",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)


def test_longmemeval_loader_sorts_sessions_and_keeps_labels_out_of_turns(tmp_path):
    path = tmp_path / "long.json"
    path.write_text(json.dumps([_long_row()]), encoding="utf-8")

    case = load_longmemeval(path)[0]

    assert [session.source_id for session in case.sessions] == ["earlier", "later"]
    assert case.sessions[0].turns == (Turn(role="user", content="I prefer tea"),)
    assert case.answer_session_ids == ("earlier",)
    assert "q-secret" not in case.case_id


def test_longmemeval_loader_accepts_official_empty_turn_content(tmp_path):
    row = _long_row()
    row["haystack_sessions"][0][0]["content"] = ""
    path = tmp_path / "long.json"
    path.write_text(json.dumps([row]), encoding="utf-8")

    assert load_longmemeval(path)[0].sessions[1].turns[0].content == ""


def test_longmemeval_loader_accepts_official_integer_answers(tmp_path):
    path = tmp_path / "long.json"
    path.write_text(json.dumps([_long_row(answer=3)]), encoding="utf-8")

    assert load_longmemeval(path)[0].answer == "3"


def test_longmemeval_loader_rejects_misaligned_haystack_fields(tmp_path):
    path = tmp_path / "long.json"
    path.write_text(
        json.dumps([_long_row(haystack_dates=["2026-01-01"])]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="equal lengths"):
        load_longmemeval(path)


def test_personamem_loader_uses_official_hashed_context_id(tmp_path):
    path = tmp_path / "questions.csv"
    _write_persona_questions(
        path,
        [
            {
                "question_id": "question-secret",
                "shared_context_id": "context-hash",
                "end_index_in_shared_context": "2",
                "user_question_or_message": "Which?",
                "all_options": "['(a) A', '(b) B']",
                "correct_answer": "(a)",
                "question_type": "preference",
            }
        ],
    )

    question = load_persona_questions(path)[0]

    assert question.shared_context_id == "context-hash"
    assert question.question == "Which?"
    assert "question-secret" not in question.case_id


def test_personamem_batches_only_ingest_new_prefix_without_future_leak(tmp_path):
    contexts_path = tmp_path / "contexts.jsonl"
    contexts_path.write_text(
        json.dumps({"context-hash": ["first", "second", "future"]}) + "\n",
        encoding="utf-8",
    )
    questions_path = tmp_path / "questions.csv"
    _write_persona_questions(
        questions_path,
        [
            {
                "question_id": "q1",
                "shared_context_id": "context-hash",
                "end_index_in_shared_context": "1",
                "user_question_or_message": "Q1",
                "all_options": "['(a) A', '(b) B']",
                "correct_answer": "(a)",
                "question_type": "x",
            },
            {
                "question_id": "q2",
                "shared_context_id": "context-hash",
                "end_index_in_shared_context": "2",
                "user_question_or_message": "Q2",
                "all_options": "['(a) A', '(b) B']",
                "correct_answer": "(b)",
                "question_type": "x",
            },
        ],
    )

    batches = list(
        persona_batches(
            load_persona_contexts(contexts_path),
            load_persona_questions(questions_path),
        )
    )

    assert batches[0][1:4] == (0, 1, ["first"])
    assert batches[1][1:4] == (1, 2, ["second"])
    assert all("future" not in batch[3] for batch in batches)


def test_pony_adapter_writes_only_neutral_transcript_fields(tmp_path):
    backend = PonyMemoryBackend(tmp_path / "memory")
    backend.ingest_sessions(
        [
            Session(
                source_id="q-secret_abs",
                date="2026-01-01",
                turns=(Turn("user", "I prefer tea"),),
            )
        ]
    )

    note = next((tmp_path / "memory" / "workspace" / "notes").iterdir())
    text = note.read_text(encoding="utf-8")

    assert "I prefer tea" in text
    assert "q-secret" not in note.name
    assert "q-secret" not in text
    assert "answer_session_ids" not in text
    assert "has_answer" not in text


def test_pony_adapter_uses_production_bm25_and_isolates_roots(tmp_path):
    first = PonyMemoryBackend(tmp_path / "first", provider_counter=lambda text: len(text))
    second = PonyMemoryBackend(tmp_path / "second", provider_counter=lambda text: len(text))
    first.ingest_sessions([Session("s1", "", (Turn("user", "favorite drink is tea"),))])
    second.ingest_sessions([Session("s2", "", (Turn("user", "favorite drink is coffee"),))])

    first_result = first.retrieve("favorite drink tea", budget_tokens=6_144)
    second_result = second.retrieve("favorite drink tea", budget_tokens=6_144)

    assert "tea" in first_result.text
    assert "coffee" in second_result.text
    assert "tea" not in second_result.text


def test_session_split_is_deterministic_and_respects_file_limit():
    content = "x" * 70_000
    session = Session("session", "", (Turn("user", content), Turn("assistant", content)))

    first = split_session_note(session)
    second = split_session_note(session)

    assert first == second
    assert len(first) == 2
    assert all(len(chunk) <= MAX_MEMORY_FILE_BYTES for chunk in first)


def test_pony_adapter_fails_closed_at_file_and_index_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "MAX_MEMORY_INDEX_FILES", 1)
    backend = PonyMemoryBackend(tmp_path / "files")
    backend.ingest_sessions([Session("one", "", (Turn("user", "one"),))])
    with pytest.raises(ValueError, match="512 files"):
        backend.ingest_sessions([Session("two", "", (Turn("user", "two"),))])

    monkeypatch.setattr(backends, "MAX_MEMORY_INDEX_BYTES", 100)
    backend = PonyMemoryBackend(tmp_path / "bytes")
    with pytest.raises(ValueError, match="2 MiB"):
        backend.ingest_sessions([Session("large", "", (Turn("user", "x" * 100),))])


def test_context_clipping_uses_one_shared_token_budget():
    accounting = TokenAccounting(lambda text: len(text))

    text, tokens, considered = clip_contexts(["abcd", "efgh", "ijkl"], accounting, 6)

    assert text == "abcd\n\nef"
    assert tokens == 6
    assert considered == 2


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def test_mem0_client_uses_documented_payloads_and_accepts_results_wrapper():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        if request.full_url.endswith("/search"):
            return _Response(json.dumps({"results": [{"memory": "tea"}]}).encode())
        return _Response(b"{}")

    client = Mem0Client("http://127.0.0.1:8000/", timeout=3, opener=opener)
    client.add(
        [{"role": "user", "content": "tea"}],
        user_id="u",
        run_id="r",
        metadata={"source_id": "s"},
    )
    rows = client.search("drink", user_id="u", run_id="r")

    assert rows == [{"memory": "tea"}]
    assert calls[0][0].endswith("/memories")
    assert calls[0][1]["metadata"] == {"source_id": "s"}
    assert calls[1][1]["filters"] == {"user_id": "u", "run_id": "r"}
    assert "user_id" not in calls[1][1]
    assert "run_id" not in calls[1][1]
    assert calls[1][1]["limit"] == 200
    assert "rerank" not in calls[1][1]
    assert calls[1][2] == 3


def test_mem0_persona_ingestion_preserves_roles_and_pairs_turns():
    calls = []

    class Client:
        def add(self, messages, **params):
            calls.append((messages, params))

    backend = Mem0Backend(Client(), user_id="u", run_id="r")
    backend.ingest_items(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
        source_prefix="context-1",
    )

    assert [call[0] for call in calls] == [
        [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        [{"role": "user", "content": "three"}],
    ]
    assert all(call[1]["user_id"] == "u" for call in calls)





def test_mem0_run_header_records_reproducibility_fingerprint():
    args = SimpleNamespace(
        benchmark="longmemeval",
        backend="mem0",
        mem0_fingerprint="mem0ai=2.0.12;embedder=text-embedding-v4:1024",
        limit=1,
        question_type=None,
        workers=4,
    )

    header = run._answer_run_header(args, {"model": "m", "transport": "t"}, "sha256:x", dirty=True, commit="c")

    assert header["backend_fingerprint"] == args.mem0_fingerprint
    assert header["workers"] == 4


def test_parallel_results_bounds_concurrency():
    active = 0
    peak = 0
    lock = Lock()

    def process(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    assert sorted(run._parallel_results(list(range(5)), 2, process)) == list(range(5))
    assert peak == 2


@pytest.mark.parametrize("value", [True, 0, 17])
def test_worker_count_fails_closed(value):
    with pytest.raises(ValueError, match="integer between 1 and 16"):
        run._validated_workers(value)


def test_publication_requires_complete_unique_scored_rows():
    rows = [
        {"case_id": "one", "correct": True, "failure": ""},
        {"case_id": "two", "correct": False, "failure": ""},
    ]

    summary, case_ids, complete = run._publication_state(rows, 2)

    assert summary["scored"] == 2
    assert case_ids == {"one", "two"}
    assert complete
    assert not run._publication_state(rows[:1], 2)[2]
    assert not run._publication_state([rows[0], dict(rows[0])], 2)[2]
    assert not run._publication_state([rows[0], {**rows[1], "correct": None}], 2)[2]
    assert not run._publication_state([rows[0], {**rows[1], "failure": "bad"}], 2)[2]


def test_report_rejects_different_pony_commits(tmp_path):
    base_run = {
        "protocol": run.PROTOCOL,
        "benchmark": "personamem",
        "dataset_sha256": "sha256:data",
        "pony_commit": "left",
        "answer_model": "model",
        "answer_transport": "transport",
        "answer_prompt_sha256": "sha256:prompt",
        "judge_model": None,
        "max_retrieved_tokens": run.MAX_RETRIEVED_TOKENS,
        "temperature": 0,
        "workers": 1,
        "limit": 1,
        "question_type": None,
        "publishable": False,
    }

    def write(name, run_header):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "record_type": run.RECORD_TYPE,
                    "format_version": run.FORMAT_VERSION,
                    "run": run_header,
                    "rows": [],
                    "summary": {},
                }
            ),
            encoding="utf-8",
        )
        return path

    left = write("left.json", base_run)
    right = write("right.json", {**base_run, "pony_commit": "right"})

    with pytest.raises(ValueError, match="pony_commit"):
        run.report(
            SimpleNamespace(left=left, right=right, format="json", output=None)
        )

def test_complete_uses_canonical_system_blocks():
    class Client:
        def complete(self, **request):
            assert request["system"] == [{"type": "text", "text": "rules"}]
            return Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "answer"}],
            )

    assert run._complete(Client(), system="rules", prompt="question", max_tokens=8) == "answer"

def test_answer_temp_workspace_is_anchored_to_output_parent(tmp_path, monkeypatch):
    calls = []

    class TemporaryDirectory:
        def __init__(self, *, prefix, dir):
            calls.append((prefix, Path(dir)))

        def __enter__(self):
            return str(tmp_path / "unused")

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(run.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(run, "load_longmemeval", lambda path: [])
    output = tmp_path / "results" / "answer.json"

    run._longmemeval_answer(
        SimpleNamespace(
            dataset_path=tmp_path / "dataset.json",
            question_type=None,
            limit=None,
            workers=1,
        ),
        None,
        {"rows": []},
        output,
    )

    assert calls == [("pony-memory-public-", output.parent.resolve())]

def test_mem0_client_rejects_malformed_search_response():
    client = Mem0Client(
        "http://127.0.0.1:8000",
        opener=lambda request, timeout: _Response(b'{"results": {}}'),
    )

    with pytest.raises(ValueError, match="result list"):
        client.search("q", user_id="u", run_id="r")


def test_persona_option_scoring_accepts_label_or_exact_option():
    options = ("Tea", "Coffee", "Water")

    assert persona_correct("A", "Tea", options)
    assert persona_correct("Option 2", "B", options)
    assert persona_correct("C", "(c)", ("(a) Tea", "(b) Coffee", "(c) Water"))
    assert not persona_correct("I think tea", "A", options)


def test_judge_parser_fails_closed_on_extra_text():
    assert parse_judge_answer("CORRECT: yes") is True
    assert parse_judge_answer(" correct: NO ") is False
    with pytest.raises(ValueError, match="did not match"):
        parse_judge_answer("Explanation\nCORRECT: yes")


def test_statistics_known_values():
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=0.001)
    assert high == pytest.approx(0.7634, abs=0.001)
    assert mcnemar_exact([True, True, False], [False, False, False]) == {
        "left_only": 2,
        "right_only": 0,
        "discordant": 2,
        "p_value": 0.5,
    }
    paired = paired_summary(
        [{"case_id": "a", "correct": True}, {"case_id": "b", "correct": False}],
        [{"case_id": "a", "correct": False}, {"case_id": "b", "correct": False}],
    )
    assert paired["delta"] == 0.5
    assert paired["win_tie_loss"] == [1, 1, 0]


def test_summary_reports_source_recall_and_mrr_when_available():
    summary = summarize_rows(
        [
            {
                "correct": True,
                "answer_source_recall": True,
                "answer_source_reciprocal_rank": 0.5,
            },
            {
                "correct": False,
                "answer_source_recall": False,
                "answer_source_reciprocal_rank": 0.0,
            },
            {"correct": True, "answer_source_recall": None},
        ]
    )

    assert summary["retrieval_scored"] == 2
    assert summary["answer_source_recall_at_k"] == 0.5
    assert summary["answer_source_mrr"] == 0.25


def test_resume_requires_exact_header_and_rejects_duplicate_cases(tmp_path):
    path = tmp_path / "artifact.json"
    payload = {
        "record_type": run.RECORD_TYPE,
        "format_version": run.FORMAT_VERSION,
        "created_at": "now",
        "run": {"protocol": run.PROTOCOL},
        "rows": [],
        "summary": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    resumed = run._resume_or_create(path, payload["run"], resume=True)
    assert resumed["created_at"] == "now"
    with pytest.raises(ValueError, match="header mismatch"):
        run._resume_or_create(path, {"protocol": "other"}, resume=True)

    payload["rows"] = [{"case_id": "same"}, {"case_id": "same"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        run._resume_or_create(path, payload["run"], resume=True)


def test_atomic_write_failure_propagates_before_next_case(tmp_path, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args[0])
        raise OSError("disk full")

    monkeypatch.setattr(run, "write_private_bytes_atomic", fail)

    with pytest.raises(OSError, match="disk full"):
        run._write_artifact(tmp_path / "result.json", {"rows": []})
    assert calls == [tmp_path / "result.json"]
