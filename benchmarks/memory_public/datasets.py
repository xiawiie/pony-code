"""Strict loaders for LongMemEval-S and PersonaMem-v1 32k."""

from __future__ import annotations

import ast
import csv
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Turn:
    role: str
    content: str


@dataclass(frozen=True)
class Session:
    source_id: str
    date: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class LongMemEvalCase:
    case_id: str
    question: str
    answer: str
    question_type: str
    question_date: str
    sessions: tuple[Session, ...]
    answer_session_ids: tuple[str, ...]


@dataclass(frozen=True)
class PersonaQuestion:
    case_id: str
    shared_context_id: str
    end_index: int
    question: str
    options: tuple[str, ...]
    correct_answer: str
    question_type: str


def opaque_id(value: object, *, prefix: str = "case") -> str:
    digest = sha256(str(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError:
        payload = [
            json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
            for line in text.splitlines()
            if line.strip()
        ]
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("dataset must contain JSON objects")
    return payload


def _required_text(row: dict, name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _longmemeval_answer(row: dict) -> str:
    value = row.get("answer")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _required_text(row, "answer")


def _turns(value: Any) -> tuple[Turn, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("session must contain turns")
    turns = []
    for turn in value:
        if not isinstance(turn, dict):
            raise ValueError("turn must be an object")
        role = _required_text(turn, "role")
        content = turn.get("content")
        if not isinstance(content, str):
            raise ValueError("turn content must be text")
        turns.append(Turn(role=role, content=content))
    return tuple(turns)


def load_longmemeval(path: str | Path) -> list[LongMemEvalCase]:
    cases = []
    seen = set()
    for row in _read_json_records(Path(path)):
        question_id = _required_text(row, "question_id")
        case_id = opaque_id(question_id)
        if case_id in seen:
            raise ValueError("duplicate LongMemEval question_id")
        seen.add(case_id)
        raw_sessions = row.get("haystack_sessions")
        source_ids = row.get("haystack_session_ids")
        dates = row.get("haystack_dates")
        if not all(isinstance(value, list) for value in (raw_sessions, source_ids, dates)):
            raise ValueError("LongMemEval haystack fields must be lists")
        if len(raw_sessions) != len(source_ids) or len(raw_sessions) != len(dates):
            raise ValueError("LongMemEval haystack fields must have equal lengths")
        sessions = []
        for source_id, date, turns in zip(source_ids, dates, raw_sessions, strict=True):
            if not isinstance(source_id, str) or not isinstance(date, str):
                raise ValueError("LongMemEval session id/date must be text")
            sessions.append(Session(source_id=source_id, date=date, turns=_turns(turns)))
        sessions.sort(key=lambda session: (session.date, session.source_id))
        answer_ids = row.get("answer_session_ids", [])
        if not isinstance(answer_ids, list) or not all(
            isinstance(value, str) for value in answer_ids
        ):
            raise ValueError("answer_session_ids must be a text list")
        cases.append(
            LongMemEvalCase(
                case_id=case_id,
                question=_required_text(row, "question"),
                answer=_longmemeval_answer(row),
                question_type=_required_text(row, "question_type"),
                question_date=_required_text(row, "question_date"),
                sessions=tuple(sessions),
                answer_session_ids=tuple(answer_ids),
            )
        )
    return cases


def load_persona_contexts(path: str | Path) -> dict[str, list[Any]]:
    contexts = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid PersonaMem context line {line_number}") from exc
            if not isinstance(value, dict) or len(value) != 1:
                raise ValueError("PersonaMem context line must contain one shared context")
            context_id, items = next(iter(value.items()))
            if not isinstance(context_id, str) or not context_id or not isinstance(items, list):
                raise ValueError("invalid PersonaMem shared context")
            if context_id in contexts:
                raise ValueError("duplicate PersonaMem shared_context_id")
            contexts[context_id] = items
    return contexts


def _csv_int(row: dict, name: str) -> int:
    value = row.get(name)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{name} must be a non-negative canonical integer")
    return parsed


def load_persona_questions(path: str | Path) -> list[PersonaQuestion]:
    questions = []
    seen = set()
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            question_id = _required_text(row, "question_id")
            context_id = _required_text(row, "shared_context_id")
            end_index = _csv_int(row, "end_index_in_shared_context")
            try:
                raw_options = ast.literal_eval(row.get("all_options", ""))
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"invalid all_options at CSV row {row_number}") from exc
            if not isinstance(raw_options, (list, tuple)) or len(raw_options) < 2:
                raise ValueError("all_options must contain at least two options")
            options = tuple(str(value).strip() for value in raw_options)
            if any(not value for value in options):
                raise ValueError("PersonaMem options must be non-empty")
            case_id = opaque_id(question_id)
            if case_id in seen:
                raise ValueError("duplicate PersonaMem question_id")
            seen.add(case_id)
            questions.append(
                PersonaQuestion(
                    case_id=case_id,
                    shared_context_id=context_id,
                    end_index=end_index,
                    question=_required_text(row, "user_question_or_message"),
                    options=options,
                    correct_answer=_required_text(row, "correct_answer"),
                    question_type=_required_text(row, "question_type"),
                )
            )
    return questions


def persona_batches(
    contexts: dict[str, list[Any]],
    questions: Iterable[PersonaQuestion],
):
    grouped: dict[str, list[PersonaQuestion]] = {}
    for question in questions:
        context = contexts.get(question.shared_context_id)
        if context is None:
            raise ValueError("shared_context_id is outside contexts file")
        if question.end_index > len(context):
            raise ValueError("end_index exceeds shared context")
        grouped.setdefault(question.shared_context_id, []).append(question)
    for context_id in sorted(grouped):
        previous_end = 0
        ordered = sorted(grouped[context_id], key=lambda item: item.end_index)
        by_end: dict[int, list[PersonaQuestion]] = {}
        for question in ordered:
            by_end.setdefault(question.end_index, []).append(question)
        for end_index, batch in by_end.items():
            delta = contexts[context_id][previous_end:end_index]
            yield context_id, previous_end, end_index, delta, tuple(batch)
            previous_end = end_index


def render_context_item(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        role = value.get("role") or value.get("speaker")
        content = value.get("content") or value.get("text")
        if isinstance(role, str) and isinstance(content, str):
            return f"{role}: {content}".strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
