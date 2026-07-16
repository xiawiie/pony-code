import pytest

from pico.agent.observability import RunArtifactError, validate_report


def test_report_contract_rejects_legacy_top_level_aliases():
    with pytest.raises(RunArtifactError) as error:
        validate_report({"run_id": "legacy"})
    assert error.value.status == "migration_required"
