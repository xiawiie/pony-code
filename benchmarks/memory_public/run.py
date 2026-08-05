"""CLI for public memory answer, judge, and paired report runs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from benchmarks.evaluation.metrics_common import (
    _decode_json_object,
    _utc_timestamp,
    _validate_record_header,
)
from benchmarks.evaluation.provider_benchmark import _resolve_benchmark_target
from pony.agent.action_codec import FinalAction, decode_action
from pony.providers.factory import build_transport_client
from pony.security.private_files import (
    ensure_private_dir,
    private_directory_identity,
    write_private_bytes_atomic,
)

from .backends import FullContextBackend, Mem0Backend, Mem0Client, PonyMemoryBackend
from .datasets import (
    load_longmemeval,
    load_persona_contexts,
    load_persona_questions,
    persona_batches,
)
from .scoring import (
    LONGMEMEVAL_JUDGE_SYSTEM,
    longmemeval_judge_prompt,
    paired_summary,
    parse_judge_answer,
    persona_correct,
    prompt_sha256,
    summarize_rows,
)


PROTOCOL = "pony-public-memory-v2"
RECORD_TYPE = "public_memory_benchmark_result"
FORMAT_VERSION = 1
MAX_RETRIEVED_TOKENS = 6_144
ANSWER_MAX_TOKENS = 1_024
JUDGE_MAX_TOKENS = 32
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_WORKERS = 16
EXPECTED_CASES = {"longmemeval": 500, "personamem": 589}

ANSWER_SYSTEM = (
    "Answer the question using only the supplied memory context. If the context does "
    "not contain enough information, say that you do not know. Return only the answer."
)
LONG_ANSWER_TEMPLATE = "Memory context:\n{context}\n\nQuestion:\n{question}\n"
PERSONA_ANSWER_TEMPLATE = (
    "Memory context:\n{context}\n\nQuestion:\n{question}\n\nOptions:\n{options}\n\n"
    "Return only the option letter."
)


def _digest_paths(paths: list[Path]) -> str:
    digest = sha256()
    for path in paths:
        data = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def _git_metadata(repo_root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return commit, dirty


def _client(target: dict):
    return build_transport_client(
        target["transport"],
        model=target["model"],
        base_url=target["base_url"],
        api_key=target["api_key"],
        timeout=300,
        auth_mode=target["auth_mode"],
        capabilities=target["capabilities"],
        temperature=0,
    )


def _complete(client, *, system: str, prompt: str, max_tokens: int) -> str:
    response = client.complete(
        system=[{"type": "text", "text": system}],
        tools=[],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    action = decode_action(response)
    if not isinstance(action, FinalAction):
        raise RuntimeError(f"provider did not return a final answer: {action.reason_code}")
    if action.truncated:
        raise RuntimeError("provider answer was truncated")
    return action.text.strip()


def _write_artifact(path: Path, payload: dict) -> None:
    rendered = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(rendered) > MAX_ARTIFACT_BYTES:
        raise ValueError("benchmark artifact exceeds 64 MiB")
    root = ensure_private_dir(path.parent)
    write_private_bytes_atomic(
        path,
        rendered,
        trusted_root=root,
        trusted_root_identity=private_directory_identity(root),
        max_existing_bytes=MAX_ARTIFACT_BYTES,
    )


def _load_artifact(path: Path) -> dict:
    payload = _decode_json_object(path.read_text(encoding="utf-8"))
    _validate_record_header(payload, RECORD_TYPE, FORMAT_VERSION)
    if not isinstance(payload.get("run"), dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("invalid public memory benchmark artifact")
    return payload


def _resume_or_create(path: Path, run: dict, *, resume: bool) -> dict:
    if path.exists():
        if not resume:
            raise FileExistsError(f"artifact exists: {path}")
        payload = _load_artifact(path)
        if payload["run"] != run:
            raise ValueError("resume header mismatch")
        case_ids = [row.get("case_id") for row in payload["rows"]]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("artifact contains duplicate case_id")
        return payload
    return {
        "record_type": RECORD_TYPE,
        "format_version": FORMAT_VERSION,
        "created_at": _utc_timestamp(),
        "run": run,
        "rows": [],
        "summary": {},
    }


def _backend(name: str, root: Path, *, client, mem0_url: str | None, user_id: str):
    counter = getattr(client, "count_tokens", None)
    if name == "pony":
        return PonyMemoryBackend(root, provider_counter=counter)
    if name == "full-context":
        return FullContextBackend(provider_counter=counter)
    if name == "mem0":
        if not mem0_url:
            raise ValueError("--mem0-url is required for mem0")
        return Mem0Backend(
            Mem0Client(mem0_url),
            user_id=user_id,
            run_id=PROTOCOL,
            provider_counter=counter,
        )
    raise ValueError("unsupported backend")


def _answer_run_header(args, target: dict, dataset_sha: str, *, dirty: bool, commit: str):
    template = PERSONA_ANSWER_TEMPLATE if args.benchmark == "personamem" else LONG_ANSWER_TEMPLATE
    return {
        "protocol": PROTOCOL,
        "benchmark": args.benchmark,
        "backend": args.backend,
        "ingest_adapter": {
            "pony": "transcript-to-notes-v1",
            "mem0": "mem0-native-v1",
            "full-context": "chronological-context-v1",
        }[args.backend],
        "backend_fingerprint": args.mem0_fingerprint if args.backend == "mem0" else None,
        "pony_commit": commit,
        "pony_dirty": dirty,
        "dataset_sha256": dataset_sha,
        "answer_model": target["model"],
        "answer_transport": target["transport"],
        "answer_prompt_sha256": prompt_sha256(ANSWER_SYSTEM, template),
        "judge_model": None,
        "max_retrieved_tokens": MAX_RETRIEVED_TOKENS,
        "temperature": 0,
        "workers": args.workers,
        "limit": args.limit,
        "question_type": args.question_type,
        "publishable": not dirty and args.limit is None and args.question_type is None,
    }


def _selected(values, args):
    selected = [
        value
        for value in values
        if args.question_type is None or value.question_type == args.question_type
    ]
    return selected[: args.limit] if args.limit is not None else selected


def _validated_workers(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_WORKERS:
        raise ValueError(f"workers must be an integer between 1 and {MAX_WORKERS}")
    return value


def _parallel_results(items, workers, process):
    workers = _validated_workers(workers)
    # ponytail: chunking bounds unpersisted paid work to one worker batch.
    for start in range(0, len(items), workers):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process, item) for item in items[start : start + workers]]
            for future in as_completed(futures):
                yield future.result()


def _publication_state(rows: list[dict], expected: int):
    summary = summarize_rows(rows)
    ids = [row.get("case_id") for row in rows]
    valid_ids = all(isinstance(case_id, str) and case_id for case_id in ids)
    unique_ids = set(ids) if valid_ids else set()
    complete = (
        len(rows) == expected
        and len(unique_ids) == expected
        and summary["scored"] == expected
        and not summary["failures"]
    )
    return summary, unique_ids, complete


def _longmemeval_answer(args, target, payload, output_path: Path):
    attempt_id = uuid4().hex
    cases = _selected(load_longmemeval(args.dataset_path), args)
    completed = {row["case_id"] for row in payload["rows"]}
    with tempfile.TemporaryDirectory(
        prefix="pony-memory-public-",
        dir=ensure_private_dir(output_path.parent),
    ) as temp:
        pending = [case for case in cases if case.case_id not in completed]

        def process(case):
            client = _client(target)
            backend = _backend(
                args.backend,
                Path(temp) / case.case_id,
                client=client,
                mem0_url=args.mem0_url,
                user_id=f"{case.case_id}-{attempt_id}",
            )
            backend.ingest_sessions(case.sessions)
            recalled = backend.retrieve(case.question, budget_tokens=MAX_RETRIEVED_TOKENS)
            prompt = LONG_ANSWER_TEMPLATE.format(
                context=recalled.text or "(no relevant memory retrieved)",
                question=case.question,
            )
            answer = _complete(
                client,
                system=ANSWER_SYSTEM,
                prompt=prompt,
                max_tokens=ANSWER_MAX_TOKENS,
            )
            answer_ids = set(case.answer_session_ids)
            source_rank = next(
                (
                    rank
                    for rank, source_id in enumerate(recalled.source_ids, 1)
                    if source_id in answer_ids
                ),
                None,
            )
            retrieval_scored = args.backend != "mem0" and bool(answer_ids)
            return case, {
                "case_id": case.case_id,
                "question_type": case.question_type,
                "question": case.question,
                "reference_answer": case.answer,
                "hypothesis": answer,
                "retrieved_tokens": recalled.tokens,
                "retrieved_source_ids": list(recalled.source_ids),
                "answer_source_recall": bool(source_rank) if retrieval_scored else None,
                "answer_source_reciprocal_rank": (1 / source_rank)
                if retrieval_scored and source_rank
                else (0.0 if retrieval_scored else None),
                "correct": None,
                "failure": "",
            }

        processed = len(completed)
        for case, row in _parallel_results(pending, args.workers, process):
            processed += 1
            payload["rows"].append(row)
            payload["summary"] = summarize_rows(payload["rows"])
            _write_artifact(output_path, payload)
            print(
                f"[{processed}/{len(cases)}] {case.case_id}: "
                f"{'ok' if not row['failure'] else 'failed'}",
                flush=True,
            )
    return payload


def _persona_answer(args, target, payload, output_path: Path):
    attempt_id = uuid4().hex
    contexts = load_persona_contexts(args.contexts_path)
    questions = _selected(load_persona_questions(args.dataset_path), args)
    completed = {row["case_id"] for row in payload["rows"]}
    total = len(questions)
    with tempfile.TemporaryDirectory(
        prefix="pony-memory-public-",
        dir=ensure_private_dir(output_path.parent),
    ) as temp:
        grouped = {}
        for context_id, previous_end, end_index, delta, batch in persona_batches(contexts, questions):
            grouped.setdefault(context_id, []).append((previous_end, end_index, delta, batch))
        pending = [
            (context_id, batches)
            for context_id, batches in grouped.items()
            if any(question.case_id not in completed for *_, batch in batches for question in batch)
        ]

        def process_context(item):
            context_id, batches = item
            client = _client(target)
            backend = _backend(
                args.backend,
                Path(temp) / f"context-{context_id}",
                client=client,
                mem0_url=args.mem0_url,
                user_id=f"context-{context_id}-{attempt_id}",
            )
            rows = []
            for previous_end, end_index, delta, batch in batches:
                backend.ingest_items(
                    delta,
                    source_prefix=f"context-{context_id}-{previous_end}-{end_index}",
                )
                for question in batch:
                    if question.case_id in completed:
                        continue
                    recalled = backend.retrieve(
                        question.question,
                        budget_tokens=MAX_RETRIEVED_TOKENS,
                    )
                    options = "\n".join(
                        f"{chr(ord('A') + index)}. {option}"
                        for index, option in enumerate(question.options)
                    )
                    prompt = PERSONA_ANSWER_TEMPLATE.format(
                        context=recalled.text or "(no relevant memory retrieved)",
                        question=question.question,
                        options=options,
                    )
                    answer = _complete(
                        client,
                        system=ANSWER_SYSTEM,
                        prompt=prompt,
                        max_tokens=32,
                    )
                    rows.append(
                        {
                            "case_id": question.case_id,
                            "question_type": question.question_type,
                            "question": question.question,
                            "options": list(question.options),
                            "reference_answer": question.correct_answer,
                            "hypothesis": answer,
                            "retrieved_tokens": recalled.tokens,
                            "correct": persona_correct(
                                answer,
                                question.correct_answer,
                                question.options,
                            ),
                            "failure": "",
                        }
                    )
            return rows

        processed = len(completed)
        for rows in _parallel_results(pending, args.workers, process_context):
            for row in rows:
                processed += 1
                payload["rows"].append(row)
                payload["summary"] = summarize_rows(payload["rows"])
                _write_artifact(output_path, payload)
                print(
                    f"[{processed}/{total}] {row['case_id']}: "
                    f"{'ok' if not row['failure'] else 'failed'}",
                    flush=True,
                )
    return payload


def answer(args):
    repo_root = Path(args.repo_root).resolve()
    target = _resolve_benchmark_target(repo_root)
    dataset_paths = [Path(args.dataset_path)]
    if args.benchmark == "personamem":
        if not args.contexts_path:
            raise ValueError("--contexts-path is required for PersonaMem")
        dataset_paths.append(Path(args.contexts_path))
    commit, dirty = _git_metadata(repo_root)
    run = _answer_run_header(
        args,
        target,
        _digest_paths(dataset_paths),
        dirty=dirty,
        commit=commit,
    )
    output_path = Path(args.output)
    payload = _resume_or_create(output_path, run, resume=args.resume)
    if args.benchmark == "longmemeval":
        _longmemeval_answer(args, target, payload, output_path)
    else:
        _persona_answer(args, target, payload, output_path)
    payload["summary"] = summarize_rows(payload["rows"])
    _write_artifact(output_path, payload)
    print(json.dumps(payload["summary"], indent=2))


def judge(args):
    source = _load_artifact(Path(args.input))
    if source["run"].get("benchmark") != "longmemeval":
        raise ValueError("judge only accepts LongMemEval artifacts")
    repo_root = Path(args.repo_root).resolve()
    target = _resolve_benchmark_target(repo_root)
    run = dict(source["run"])
    run.update(
        judge_model=target["model"],
        judge_transport=target["transport"],
        judge_prompt_sha256=prompt_sha256(
            LONGMEMEVAL_JUDGE_SYSTEM,
            "question/reference/hypothesis-v1",
        ),
    )
    output_path = Path(args.output)
    payload = _resume_or_create(output_path, run, resume=args.resume)
    completed = {row["case_id"] for row in payload["rows"]}
    pending = [row for row in source["rows"] if row["case_id"] not in completed]

    def process(row):
        judged = dict(row)
        if row.get("failure"):
            judged["correct"] = None
        else:
            client = _client(target)
            response = _complete(
                client,
                system=LONGMEMEVAL_JUDGE_SYSTEM,
                prompt=longmemeval_judge_prompt(
                    row["question"],
                    row["reference_answer"],
                    row["hypothesis"],
                ),
                max_tokens=JUDGE_MAX_TOKENS,
            )
            judged["judge_response"] = response
            judged["correct"] = parse_judge_answer(response)
        return judged

    processed = len(completed)
    workers = _validated_workers(source["run"].get("workers", 1))
    for judged in _parallel_results(pending, workers, process):
        processed += 1
        payload["rows"].append(judged)
        payload["summary"] = summarize_rows(payload["rows"])
        _write_artifact(output_path, payload)
        print(f"[{processed}/{len(source['rows'])}] {judged['case_id']}", flush=True)
    print(json.dumps(payload["summary"], indent=2))


def report(args):
    left = _load_artifact(Path(args.left))
    right = _load_artifact(Path(args.right))
    comparable = (
        "protocol",
        "benchmark",
        "dataset_sha256",
        "pony_commit",
        "answer_model",
        "answer_transport",
        "answer_prompt_sha256",
        "judge_model",
        "judge_transport",
        "max_retrieved_tokens",
        "temperature",
        "workers",
        "limit",
        "question_type",
    )
    mismatches = [key for key in comparable if left["run"].get(key) != right["run"].get(key)]
    if mismatches:
        raise ValueError(f"artifacts are not comparable: {', '.join(mismatches)}")
    benchmark = left["run"]["benchmark"]
    expected = EXPECTED_CASES.get(benchmark)
    if expected is None:
        raise ValueError("unsupported benchmark")
    left_summary, left_ids, left_complete = _publication_state(left["rows"], expected)
    right_summary, right_ids, right_complete = _publication_state(right["rows"], expected)
    same_case_ids = left_ids == right_ids
    result = {
        "benchmark": left["run"]["benchmark"],
        "left": {
            "backend": left["run"]["backend"],
            "summary": left_summary,
        },
        "right": {
            "backend": right["run"]["backend"],
            "summary": right_summary,
        },
        "paired": paired_summary(left["rows"], right["rows"]),
        "expected_cases": expected,
        "same_case_ids": same_case_ids,
        "publishable": bool(
            left["run"].get("publishable") and right["run"].get("publishable")
            and left_complete
            and right_complete
            and same_case_ids
        ),
    }
    if args.format == "json":
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        paired = result["paired"]
        rendered = (
            "| Benchmark | Backend | Accuracy | 95% Wilson CI | N |\n"
            "|---|---:|---:|---:|---:|\n"
            f"| {result['benchmark']} | {result['left']['backend']} | "
            f"{left_summary['accuracy']:.2%} | "
            f"[{left_summary['wilson_95'][0]:.2%}, {left_summary['wilson_95'][1]:.2%}] | "
            f"{left_summary['scored']} |\n"
            f"| {result['benchmark']} | {result['right']['backend']} | "
            f"{right_summary['accuracy']:.2%} | "
            f"[{right_summary['wilson_95'][0]:.2%}, {right_summary['wilson_95'][1]:.2%}] | "
            f"{right_summary['scored']} |\n\n"
            f"Paired delta: {paired['delta']:+.2%}; "
            f"paired cases: {paired['paired']}/{expected}; "
            f"win/tie/loss: {paired['win_tie_loss']}; "
            f"McNemar p={paired['mcnemar_exact']['p_value']:.4g}; "
            f"publishable={str(result['publishable']).lower()}\n"
        )
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    answer_parser = commands.add_parser("answer")
    answer_parser.add_argument("--benchmark", choices=("longmemeval", "personamem"), required=True)
    answer_parser.add_argument("--backend", choices=("pony", "mem0", "full-context"), required=True)
    answer_parser.add_argument("--dataset-path", required=True)
    answer_parser.add_argument("--contexts-path")
    answer_parser.add_argument("--output", required=True)
    answer_parser.add_argument("--repo-root", default=".")
    answer_parser.add_argument("--mem0-url")
    answer_parser.add_argument("--mem0-fingerprint")
    answer_parser.add_argument("--limit", type=int)
    answer_parser.add_argument("--question-type")
    answer_parser.add_argument("--workers", type=int, default=1)
    answer_parser.add_argument("--resume", action="store_true")
    answer_parser.set_defaults(handler=answer)

    judge_parser = commands.add_parser("judge")
    judge_parser.add_argument("--input", required=True)
    judge_parser.add_argument("--output", required=True)
    judge_parser.add_argument("--repo-root", default=".")
    judge_parser.add_argument("--resume", action="store_true")
    judge_parser.set_defaults(handler=judge)

    report_parser = commands.add_parser("report")
    report_parser.add_argument("--left", required=True)
    report_parser.add_argument("--right", required=True)
    report_parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    report_parser.add_argument("--output")
    report_parser.set_defaults(handler=report)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "answer" and args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be positive")
    if args.command == "answer":
        _validated_workers(args.workers)
    if args.command == "answer" and args.backend == "mem0" and not args.mem0_fingerprint:
        raise ValueError("--mem0-fingerprint is required for mem0")
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
