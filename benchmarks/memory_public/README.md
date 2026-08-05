# Public Memory Benchmark: LongMemEval + PersonaMem + mem0

This directory evaluates Pony's existing memory retrieval against public memory
benchmarks without adding benchmark code or dependencies to the `pony` runtime package.

The precise system name used in reports is:

> **Pony User-Notes/BM25, transcript-to-notes benchmark adapter**

It must not be described as automatic production ingestion of every conversation. The
adapter is a benchmark-only bridge from public transcript data to Pony's existing User
Notes format. The production retrieval path itself is unchanged:

```text
BlockStore -> Retrieval.snapshot() -> recall_candidates()
```

## Scope

The harness supports:

| Benchmark | Dataset | Questions | Scoring |
|---|---|---:|---|
| LongMemEval | LongMemEval-S cleaned V1 | 500 | deterministic LLM judge |
| PersonaMem | PersonaMem-v1 32k adaptation | 589 | multiple-choice exact match |

Backends:

- `pony`: neutral transcript notes plus Pony's production BM25 recall;
- `mem0`: mem0 OSS native `/memories` and `/search` HTTP calls;
- `full-context`: chronological context reference under the same reader budget.

`full-context` is a reference ceiling, not a third memory product. PersonaMem results
must be named **PersonaMem-v1 32k, retrieval-augmented adaptation**. They are not the
official full-context leaderboard setting.

## Frozen protocol (`pony-public-memory-v2`)

V2 supersedes the earlier smoke-only V1 artifacts by recording bounded concurrency,
aborting resumably on live failures, and requiring complete paired evidence before a
report is publishable.

- answer model temperature: `0`;
- LongMemEval judge temperature: `0`;
- reader/retrieval budget: `6144` tokens;
- Pony retrieval defaults: top 6, maximum 1024 tokens per note;
- mem0 search: top 200, then the same `TokenAccounting` budget clipping;
- one process, fixed dataset selection, no model retry;
- `--workers` bounds independent LongMemEval cases or PersonaMem shared contexts to
  `1..16`; order inside each case/context remains strict;
- one independent memory root/user per LongMemEval question;
- one independent memory root/user per PersonaMem shared context;
- atomic artifact updates for each returned question row;
- dataset, prompt, protocol, model, worker count, Pony commit, and dirty state recorded.

A run configuration is publication-eligible only when it uses the complete dataset, has
no question-type filter, and starts from a clean Pony checkout. `--limit` and
`--question-type` runs are smoke/debug evidence only. The final report additionally
requires both sides to use the same Pony commit and transports, contain the exact
expected unique case set, score every case, and contain no failures.

## Leakage controls

LongMemEval only sends `role` and `content` into memory. These fields never enter memory
or the answer prompt:

- `answer`;
- `has_answer`;
- `answer_session_ids`;
- `question_id` or question-derived tags;
- question type labels.

Filesystem names and mem0 user IDs use opaque hashes. This matters because some public
question IDs encode labels such as `_abs`.

PersonaMem follows the official prefix boundary:

```text
for each shared_context_id:
    sort questions by end_index_in_shared_context
    ingest context[previous_end:end_index]
    answer every question at that end_index
```

It never ingests a full shared context before answering an earlier question. Question,
options, correct answer, and question type are answer/scoring data, not memory data.

## Data layout

Datasets and generated artifacts are ignored by Git:

```text
benchmarks/memory_public/data/
benchmarks/memory_public/results/
```

Expected inputs:

```text
benchmarks/memory_public/data/longmemeval_s_cleaned_v1.json
benchmarks/memory_public/data/personamem/questions_32k.csv
benchmarks/memory_public/data/personamem/shared_contexts_32k.jsonl
```

Acquire the files from the official LongMemEval and PersonaMem repositories/dataset
pages. Do not commit datasets, API keys, mem0 data, or result artifacts.

## Cost gate

`answer` and `judge` make real Provider requests using the repository's existing `.env`
resolution path. They are paid/live operations and must only be run after explicit
approval for that invocation. `report` is offline.

The harness intentionally reuses:

- `pony.config.environment.read_project_env`;
- `pony.config.model.resolve_model_config`;
- `pony.providers.factory.build_transport_client`.

There is no benchmark-specific Provider fallback or second configuration format.

## Pinned mem0 OSS deployment

The comparison server is pinned independently from Pony:

- `mem0/memory-benchmarks` commit `4b61c5d31b9c668a12b4f5e78064248a02c82d2b`;
- `mem0ai==2.0.12` (`v2.0.12`, commit
  `42cf18c4e6adb448e981aa1c7b55c1602b0cb670`);
- `qdrant/qdrant:v1.18.0`;
- answer/extraction model `qwen3.7-max`, temperature `0`;
- embedder `text-embedding-v4`, observed output dimension `1024`;
- benchmark server bound to loopback port `8888`; Qdrant remains Docker-network only.

The benchmark repository commit references the deleted mem0 branch
`feat/v3-pipeline`, so the image replaces that floating dependency with the stable
`mem0ai==2.0.12` release. That release rejects top-level identity parameters during
search; this harness therefore sends `filters={user_id, run_id}` to `/search`. The
embedder config omits `embedding_dims` because the current OpenAI-compatible gateway
rejects the `dimensions` request parameter; Qdrant is configured separately with
`embedding_model_dims: 1024`. No API key or API base is written to benchmark artifacts.

Every mem0 run requires an explicit deployment fingerprint. For the pinned deployment
used on August 4, 2026:

```bash
MEM0_FINGERPRINT='memory-benchmarks@4b61c5d;mem0ai=2.0.12;llm=qwen3.7-max;embedder=text-embedding-v4:1024;qdrant=1.18.0;mem0-image=sha256:65c87fca7ad237bb562befa086e9dd2f017f7a6139305dd458341c4797a2718c;qdrant-image=sha256:b3063c673f3973877c038eeecc392bad5011f072ee7892b56c9a8e204a3bdea9;config-sha256=1a19b22ec080e56ab60a7d6aac01838578f95b7d46d15b9196808e7038d2108d;search-scope=filters;persona-ingest=role-pairs'
```

LongMemEval preserves the official mem0 conversation-pair ingestion protocol: the full
500-question run performs 124,345 extraction `add` calls. PersonaMem preserves roles
and uses the same pair boundary; the full 589-question adaptation performs 3,547
`add` calls over 6,425 context items. These counts are part of the cost/time estimate,
not a reason to batch multiple conversations into a different protocol.

## Commands

### 1. Smoke test Pony on LongMemEval

```bash
uv run --frozen python -m benchmarks.memory_public.run answer \
  --benchmark longmemeval \
  --backend pony \
  --dataset-path benchmarks/memory_public/data/longmemeval_s_cleaned_v1.json \
  --output benchmarks/memory_public/results/longmemeval-pony-smoke.json \
  --limit 10
```

### 2. Complete LongMemEval answer runs

```bash
uv run --frozen python -m benchmarks.memory_public.run answer \
  --benchmark longmemeval \
  --backend pony \
  --dataset-path benchmarks/memory_public/data/longmemeval_s_cleaned_v1.json \
  --workers 4 \
  --output benchmarks/memory_public/results/longmemeval-pony-answers.json

uv run --frozen python -m benchmarks.memory_public.run answer \
  --benchmark longmemeval \
  --backend mem0 \
  --dataset-path benchmarks/memory_public/data/longmemeval_s_cleaned_v1.json \
  --mem0-url http://127.0.0.1:8888 \
  --mem0-fingerprint "$MEM0_FINGERPRINT" \
  --workers 4 \
  --output benchmarks/memory_public/results/longmemeval-mem0-answers.json
```

Use `--resume` only with an existing artifact whose complete run header matches. A
mismatch fails closed instead of mixing protocols or models. Each process uses fresh
mem0 user IDs, so a timed-out partial write is never retried against the same identity;
resume replays that case/context from its start. A live ingestion, answer, or judge error
aborts the invocation instead of permanently converting an infrastructure failure into
an incorrect benchmark row.

### 3. Judge LongMemEval

Answer generation and judging are separate so an answer model is never silently called
again during scoring. Judge runs inherit the answer artifact's worker count.

```bash
uv run --frozen python -m benchmarks.memory_public.run judge \
  --input benchmarks/memory_public/results/longmemeval-pony-answers.json \
  --output benchmarks/memory_public/results/longmemeval-pony-judged.json

uv run --frozen python -m benchmarks.memory_public.run judge \
  --input benchmarks/memory_public/results/longmemeval-mem0-answers.json \
  --output benchmarks/memory_public/results/longmemeval-mem0-judged.json
```

The judge accepts exactly `CORRECT: yes` or `CORRECT: no`; malformed output aborts for
explicit `--resume` instead of being guessed or scored.

### 4. PersonaMem-v1 32k

```bash
uv run --frozen python -m benchmarks.memory_public.run answer \
  --benchmark personamem \
  --backend pony \
  --dataset-path benchmarks/memory_public/data/personamem/questions_32k.csv \
  --contexts-path benchmarks/memory_public/data/personamem/shared_contexts_32k.jsonl \
  --workers 4 \
  --output benchmarks/memory_public/results/personamem-pony.json

uv run --frozen python -m benchmarks.memory_public.run answer \
  --benchmark personamem \
  --backend mem0 \
  --dataset-path benchmarks/memory_public/data/personamem/questions_32k.csv \
  --contexts-path benchmarks/memory_public/data/personamem/shared_contexts_32k.jsonl \
  --mem0-url http://127.0.0.1:8888 \
  --mem0-fingerprint "$MEM0_FINGERPRINT" \
  --workers 4 \
  --output benchmarks/memory_public/results/personamem-mem0.json
```

PersonaMem is scored during `answer`, because the reference is a multiple-choice option.

### 5. Paired report

```bash
uv run --frozen python -m benchmarks.memory_public.run report \
  --left benchmarks/memory_public/results/longmemeval-pony-judged.json \
  --right benchmarks/memory_public/results/longmemeval-mem0-judged.json \
  --format markdown \
  --output benchmarks/memory_public/results/longmemeval-comparison.md
```

The report checks that protocol, Pony commit, dataset digest, answer/judge model and
transport, prompts, reader budget, temperature, worker count, and filters match. It also
requires the complete expected case set with no duplicate, unscored, or failed rows
before returning `publishable=true`. It reports:

- accuracy and 95% Wilson interval;
- paired accuracy delta and deterministic paired-bootstrap interval;
- win/tie/loss counts;
- exact McNemar p-value;
- LongMemEval answer-source Recall@k/MRR when source metadata is trustworthy;
- a `publishable` flag.

## Artifact format

Each JSON artifact is a versioned record:

```json
{
  "record_type": "public_memory_benchmark_result",
  "format_version": 1,
  "run": {
    "protocol": "pony-public-memory-v2",
    "benchmark": "longmemeval",
    "backend": "pony",
    "ingest_adapter": "transcript-to-notes-v1",
    "dataset_sha256": "sha256:...",
    "pony_commit": "...",
    "pony_dirty": false,
    "answer_model": "...",
    "answer_prompt_sha256": "sha256:...",
    "judge_model": null,
    "max_retrieved_tokens": 6144,
    "temperature": 0,
    "workers": 4,
    "publishable": true
  },
  "rows": [],
  "summary": {}
}
```

Legacy/source failure rows remain visible with `correct: null` and a stable failure
string. New live failures abort for explicit `--resume`. Work is scheduled in chunks no
larger than `--workers`; an artifact write failure prevents the next chunk from starting,
although the rest of the current in-flight chunk may finish.

## Result table template

Do not fill or publish this table until complete, clean, comparable runs finish:

| Benchmark | Setting | Pony User-Notes/BM25 | mem0 OSS | Paired delta | N |
|---|---|---:|---:|---:|---:|
| LongMemEval-S cleaned V1 | 6144-token retrieval | TBD | TBD | TBD | 500 |
| PersonaMem-v1 32k adaptation | 6144-token retrieval | TBD | TBD | TBD | 589 |

Recommended résumé wording after verification:

> Built a reproducible public evaluation harness for my agent memory system on
> LongMemEval and PersonaMem, comparing Pony User-Notes/BM25 with mem0 OSS under a
> shared answer model and token budget; recorded paired confidence intervals and
> resumable per-question artifacts.

Only replace this with numeric claims after preserving the exact artifacts, clean commit,
dataset digests, model identities, and report output.

## Known limits

- Pony ingestion here is a benchmark adapter, not production automatic memory writing.
- mem0 metadata round-trip varies by deployment. Retrieval Recall/MRR is therefore only
  meaningful when the deployment demonstrably returns source metadata; answer accuracy
  remains the primary comparison.
- Full-context takes the most recent chronological material that fits the 6144-token
  reader budget. It is diagnostic, not an oracle with unlimited context.
- Concurrency is only across independent cases/contexts. PersonaMem prefix ingestion and
  LongMemEval conversation-pair ingestion remain sequential inside each worker. There is
  deliberately no model retry, dynamic backend registry, or extra dependency.

## Offline verification

```bash
uv run --frozen ruff check benchmarks/memory_public tests/test_public_memory_benchmark.py
uv run --frozen pytest -q \
  tests/test_public_memory_benchmark.py \
  tests/test_memory_quality_benchmark.py \
  tests/test_fixed_benchmark.py
```
