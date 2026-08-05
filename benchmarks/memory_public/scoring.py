"""Scoring and paired statistics for public memory benchmark artifacts."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import math
import random
import re


LONGMEMEVAL_JUDGE_SYSTEM = (
    "You are an impartial evaluator. Decide whether the hypothesis correctly answers "
    "the question using the reference answer. Accept equivalent wording and concise "
    "answers; reject contradictions, missing required facts, and unsupported guesses. "
    "Return exactly one line: CORRECT: yes or CORRECT: no."
)


def longmemeval_judge_prompt(question: str, reference: str, hypothesis: str) -> str:
    # Compatible with the public LongMemEval yes/no evaluation contract while using
    # Pony's configured Provider instead of copying its vendor-specific client.
    return (
        f"Question:\n{question}\n\n"
        f"Reference answer:\n{reference}\n\n"
        f"Hypothesis:\n{hypothesis}\n"
    )


def parse_judge_answer(text: str) -> bool:
    match = re.fullmatch(r"\s*CORRECT:\s*(yes|no)\s*", str(text), re.IGNORECASE)
    if not match:
        raise ValueError("judge response did not match CORRECT: yes|no")
    return match.group(1).lower() == "yes"


def normalize_option_answer(answer: str, options: tuple[str, ...]) -> int | None:
    value = str(answer).strip()
    if not value:
        return None
    folded = value.casefold().rstrip(".!?")
    for index, option in enumerate(options):
        if folded == option.strip().casefold().rstrip(".!?"):
            return index
    match = re.fullmatch(
        r"(?:option\s*)?\(?([A-Za-z]|\d+)\)?[.):]?",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    label = match.group(1)
    index = int(label) - 1 if label.isdigit() else ord(label.upper()) - ord("A")
    return index if 0 <= index < len(options) else None


def persona_correct(hypothesis: str, correct_answer: str, options: tuple[str, ...]) -> bool:
    predicted = normalize_option_answer(hypothesis, options)
    expected = normalize_option_answer(correct_answer, options)
    return predicted is not None and expected is not None and predicted == expected


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054):
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def paired_bootstrap_interval(
    left: list[bool],
    right: list[bool],
    *,
    samples: int = 10_000,
    seed: int = 0,
):
    if len(left) != len(right):
        raise ValueError("paired vectors must have equal length")
    if not left:
        return 0.0, 0.0
    differences = [float(a) - float(b) for a, b in zip(left, right, strict=True)]
    rng = random.Random(seed)
    values = sorted(
        sum(differences[rng.randrange(len(differences))] for _ in differences)
        / len(differences)
        for _ in range(samples)
    )
    return values[int(samples * 0.025)], values[min(samples - 1, int(samples * 0.975))]


def mcnemar_exact(left: list[bool], right: list[bool]) -> dict:
    if len(left) != len(right):
        raise ValueError("paired vectors must have equal length")
    left_only = sum(a and not b for a, b in zip(left, right, strict=True))
    right_only = sum(b and not a for a, b in zip(left, right, strict=True))
    discordant = left_only + right_only
    if not discordant:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, value)
            for value in range(0, min(left_only, right_only) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return {
        "left_only": left_only,
        "right_only": right_only,
        "discordant": discordant,
        "p_value": p_value,
    }


def summarize_rows(rows: list[dict]) -> dict:
    scored = [row for row in rows if isinstance(row.get("correct"), bool)]
    successes = sum(row["correct"] for row in scored)
    low, high = wilson_interval(successes, len(scored))
    failures = Counter(row.get("failure", "") for row in rows if row.get("failure"))
    retrieval_rows = [
        row for row in rows if isinstance(row.get("answer_source_recall"), bool)
    ]
    reciprocal_ranks = [
        float(row.get("answer_source_reciprocal_rank", 0.0)) for row in retrieval_rows
    ]
    return {
        "total": len(rows),
        "scored": len(scored),
        "correct": successes,
        "accuracy": successes / len(scored) if scored else 0.0,
        "wilson_95": [low, high],
        "retrieval_scored": len(retrieval_rows),
        "answer_source_recall_at_k": (
            sum(row["answer_source_recall"] for row in retrieval_rows)
            / len(retrieval_rows)
            if retrieval_rows
            else None
        ),
        "answer_source_mrr": (
            sum(reciprocal_ranks) / len(reciprocal_ranks)
            if reciprocal_ranks
            else None
        ),
        "failures": dict(sorted(failures.items())),
    }


def paired_summary(left_rows: list[dict], right_rows: list[dict]) -> dict:
    left_by_id = {row["case_id"]: row for row in left_rows}
    right_by_id = {row["case_id"]: row for row in right_rows}
    ids = sorted(left_by_id.keys() & right_by_id.keys())
    pairs = [
        (left_by_id[case_id].get("correct"), right_by_id[case_id].get("correct"))
        for case_id in ids
    ]
    pairs = [(left, right) for left, right in pairs if type(left) is type(right) is bool]
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    wins = sum(a and not b for a, b in pairs)
    losses = sum(b and not a for a, b in pairs)
    ties = len(pairs) - wins - losses
    delta = (sum(left) - sum(right)) / len(pairs) if pairs else 0.0
    return {
        "paired": len(pairs),
        "left_accuracy": sum(left) / len(left) if left else 0.0,
        "right_accuracy": sum(right) / len(right) if right else 0.0,
        "delta": delta,
        "delta_bootstrap_95": list(paired_bootstrap_interval(left, right)),
        "win_tie_loss": [wins, ties, losses],
        "mcnemar_exact": mcnemar_exact(left, right),
    }


def prompt_sha256(system: str, prompt_template: str) -> str:
    return "sha256:" + sha256(f"{system}\n{prompt_template}".encode("utf-8")).hexdigest()
