import json

from benchmarks.evaluation.efficiency_evaluation import (
    TTFT_STATUS,
    _TimedProvider,
    _aggregate_compaction_decision,
    _latency_stats,
    _paired_savings,
    _provider_failure_evidence,
    _scenario_decision,
    run_efficiency_evaluation,
)
from pony.providers.response import Response, StopReason


_ACTIVE = (
    "GOAL-7Q4M DECISION-CURRENT-9X2P CONSTRAINT-6D3R src/engine.py "
    "ERROR-E214 TASK-U5N7 NEXT-R8C2"
)


class _DeterministicProvider:
    model = "offline-evaluation"
    supports_prompt_cache = False

    def __init__(self):
        self.last_transport_attempts = 1
        self._tool_index = 0

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        del tools, max_tokens, cache_breakpoints
        system_text = "\n".join(
            str(block.get("text", ""))
            for block in system
            if isinstance(block, dict)
        )
        input_tokens = max(1, len(json.dumps(messages, ensure_ascii=False)) // 4)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": 20,
            "total_tokens": input_tokens + 20,
            "cached_tokens": 0,
        }
        self.last_transport_attempts = 1
        if "You compact coding-agent history" in system_text:
            summary = (
                "# Goal\nGOAL-7Q4M\n"
                "# Constraints & Preferences\nCONSTRAINT-6D3R\n"
                "# Progress\n## Done\nNone\n## In Progress\nTASK-U5N7\n"
                "## Blocked\nERROR-E214\n"
                "# Key Decisions\nDECISION-CURRENT-9X2P\n"
                "# Next Steps\nNEXT-R8C2\n"
                "# Critical Context\nGOAL-7Q4M\n"
                "# Files & Errors\nsrc/engine.py ERROR-E214"
            )
            return Response(
                StopReason.END_TURN,
                [{"type": "text", "text": summary}],
                usage,
            )

        last_content = json.dumps(messages[-1].get("content"), ensure_ascii=False)
        has_recent_tool_result = any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"]
            )
            for message in messages[-2:]
        )
        if "latency.txt" in last_content and not has_recent_tool_result:
            self._tool_index += 1
            return Response(
                StopReason.TOOL_USE,
                [
                    {
                        "type": "tool_use",
                        "id": f"offline_tool_{self._tool_index}",
                        "name": "read_file",
                        "input": {"path": "latency.txt", "start": 1, "end": 10},
                    }
                ],
                usage,
            )
        if has_recent_tool_result:
            answer = "LATENCY-TOOL-3N8Q"
        elif "LATENCY-LONG-5V7D" in last_content:
            answer = "LATENCY-LONG-5V7D"
        elif "LATENCY-FINAL-4K2M" in last_content:
            answer = "LATENCY-FINAL-4K2M"
        else:
            answer = _ACTIVE
        return Response(
            StopReason.END_TURN,
            [{"type": "text", "text": answer}],
            usage,
        )


def _condition(*, scc=True, complete=True, failures=0, totals=(100, 10)):
    return {
        "scc": scc,
        "usage_complete": complete,
        "provider_failures": failures,
        "rows": [
            {
                "usage": {
                    "input_tokens": totals[0],
                    "output_tokens": totals[1],
                    "total_tokens": sum(totals),
                }
            }
        ],
    }


def test_paired_savings_charges_summary_cost_and_finds_break_even():
    baseline = {
        "rows": [
            {"usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}},
            {"usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}},
        ]
    }
    compacted = {
        "rows": [
            {"usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}},
            {"usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}},
        ]
    }
    summary_calls = [
        {
            "completed": True,
            "usage": {"input_tokens": 70, "output_tokens": 10, "total_tokens": 80},
        }
    ]

    result = _paired_savings(baseline, compacted, summary_calls)

    assert result["break_even_turn"] == 2
    assert result["cumulative"][0]["net_saved_tokens"] == -20
    assert result["net_saved_tokens_at_horizon"] == 40


def test_compaction_decision_is_inconclusive_without_complete_usage():
    baseline = _condition()
    compacted = _condition(complete=False)
    savings = {"break_even_turn": 1}
    summary_calls = [
        {"completed": True, "usage": {"input_tokens": 10, "output_tokens": 2}}
    ]

    assert (
        _scenario_decision(baseline, compacted, savings, summary_calls)
        == "inconclusive"
    )


def test_compaction_repetitions_allow_one_invalid_pair_but_never_hide_rejection():
    assert (
        _aggregate_compaction_decision(
            [
                {"decision": "accept"},
                {"decision": "inconclusive"},
                {"decision": "accept"},
            ]
        )
        == "accept"
    )
    assert (
        _aggregate_compaction_decision(
            [
                {"decision": "accept"},
                {"decision": "reject"},
                {"decision": "accept"},
            ]
        )
        == "reject"
    )


def test_timed_provider_records_only_bounded_evidence():
    recorder = _TimedProvider(_DeterministicProvider())

    recorder.complete(
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "private prompt"}],
        max_tokens=16,
    )

    assert recorder.calls[0]["completed"] is True
    assert recorder.calls[0]["provider_complete_ms"] >= 0
    assert recorder.calls[0]["transport_attempts"] == 1
    assert "messages" not in recorder.calls[0]
    assert "response" not in recorder.calls[0]


def test_provider_failure_evidence_reports_only_count_and_error_types():
    evidence = _provider_failure_evidence(
        [
            {"completed": True},
            {"completed": False, "error_type": "ProviderTransportError"},
        ]
    )

    assert evidence == {
        "provider_failures": 1,
        "provider_error_types": ["ProviderTransportError"],
    }


def test_latency_stats_use_interpolated_p50_and_p95():
    assert _latency_stats([10, 20, 30, 40]) == {
        "count": 4,
        "min_ms": 10.0,
        "p50_ms": 25.0,
        "p95_ms": 38.5,
        "max_ms": 40.0,
    }


def test_offline_efficiency_evaluation_exercises_compaction_resume_and_tools(tmp_path):
    payload = run_efficiency_evaluation(
        compaction_repetitions=3,
        latency_repetitions=1,
        client_factory=_DeterministicProvider,
        target={
            "provider": "offline",
            "transport": "offline",
            "variant": "offline",
            "model": "offline-evaluation",
        },
        source_root=tmp_path,
    )

    assert payload["record_type"] == "efficiency_evaluation_result"
    assert payload["compaction"]["measured_decision"] == "accept"
    assert payload["compaction"]["decision"] == "inconclusive"
    assert [
        scenario["scenario"] for scenario in payload["compaction"]["scenarios"]
    ] == ["single_compaction_resume", "repeated_compaction_resume"]
    for scenario in payload["compaction"]["scenarios"]:
        assert scenario["accepted_trials"] == 3
        for trial in scenario["trials"]:
            assert trial["baseline"]["scc"] is True
            assert trial["compacted"]["scc"] is True
            assert trial["savings"]["break_even_turn"] is not None
            assert trial["savings"]["net_saved_tokens_at_horizon"] > 0
    assert payload["latency"]["ttft_status"] == TTFT_STATUS
    assert payload["latency"]["success_rate"] == 1.0
    assert payload["latency"]["provider_failures"] == 0
    tool_rows = [
        row
        for row in payload["latency"]["rows"]
        if row["workload"] == "tool_continuation"
    ]
    assert tool_rows[0]["read_file_calls"] == 1
