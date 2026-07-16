from tests.test_observability_contract import _report

from pico.agent.observability import render_summary_text


def test_summary_is_derived_from_current_report_without_content():
    text = render_summary_text(_report())
    assert "Run run_1" in text
    assert "10 input / 2 output tokens" in text
    assert "secret" not in text
